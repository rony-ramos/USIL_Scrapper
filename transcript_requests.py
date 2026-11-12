"""Importar cURL reales de VTT: solicitudes solo en memoria, nunca como comandos."""
from datetime import datetime, timedelta, timezone
import glob
import hashlib
import json
from pathlib import Path
import re
import shlex
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

from canvas_sync import write_json
from knowledge_core import archive_text, inside, load_json, parse_vtt, transcript_verified
from zoom_capture import transcript_request, viewer_reference


def parse_curls(text):
    starts = list(re.finditer(r'\bcurl\s+--url\s+', text))
    if not starts:
        raise ValueError('No se encontraron solicitudes curl --url')
    parsed = []
    for index, start in enumerate(starts):
        chunk = text[start.start():starts[index+1].start() if index+1 < len(starts) else len(text)]
        lines = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line.startswith(('curl ', '-')):
                continue
            line = re.sub(r'(\^")[\s\]^]*$', r'\1', line)
            line = re.sub(r"(['\"])\s*\]\s*$", r'\1', line)
            line = re.sub(r'\s+[\\^]\s*$', '', line)
            lines.append(line)
        try:
            args = shlex.split('\n'.join(lines).replace('^', ''))
        except ValueError:
            raise ValueError('Sintaxis cURL incompleta; copiar la solicitud completa') from None
        url, headers, position = None, {}, 1
        while position < len(args):
            flag = args[position]
            if flag == '--compressed':
                position += 1
                continue
            if flag not in ('--url', '-H', '--header', '-b', '--cookie', '-X', '--request') or position+1 >= len(args):
                raise ValueError('Opcion cURL no admitida; se aceptan solo solicitudes GET de VTT')
            value = args[position+1]
            if flag == '--url':
                if url is not None:
                    raise ValueError('Cada cURL requiere exactamente una URL')
                url = value
            elif flag in ('-X', '--request'):
                if value.upper() != 'GET':
                    raise ValueError('Solo se admiten solicitudes GET')
            elif flag in ('-b', '--cookie'):
                headers['cookie'] = value
            elif ':' in value:
                name, value = value.split(':', 1)
                headers[name.strip().lower()] = value.strip()
            elif value.endswith(';'):
                headers[value[:-1].strip().lower()] = ''
            else:
                raise ValueError('Cabecera cURL no valida')
            position += 2
        if not url or not transcript_request(url):
            raise ValueError('Solo se admiten URLs HTTPS de transcripciones Zoom observadas')
        if any('\r' in k+v or '\n' in k+v for k, v in headers.items()):
            raise ValueError('Cabeceras cURL no validas')
        fid = parse_qs(urlsplit(url).query).get('fid', [])
        if len(fid) != 1 or not fid[0]:
            raise ValueError('La solicitud VTT requiere un fid observado')
        reference = viewer_reference(headers['referer']) if headers.get('referer') else None
        parsed.append({'url': url, 'headers': headers,
                       'id': hashlib.sha256(fid[0].encode()).hexdigest()[:20], 'reference': reference})
    return parsed


def associate_pending(context, dry_run=False):
    """Relacionar por identidad de visor; no inferir fechas por orden o similitud."""
    root = context['root']
    classes = [(p, load_json(p)) for p in root.glob('semanas/*/clases/*/clase.json')]
    reports = []
    for source_path in sorted(root.glob('transcripciones-sin-fecha/*/fuente.json')):
        source = load_json(source_path)
        if source.get('status') != 'pending_date':
            continue
        reference = source.get('recording_reference')
        matches = {}
        if reference:
            for class_path, row in classes:
                files = [f for f in row.get('zoom_detail', {}).get('files', [])
                         if f.get('recording_reference') == reference]
                if files:
                    matches[row['id']] = (class_path, row, files)
        reason = 'metadatos_del_visor_pendientes' if not matches else 'visor_coincide_con_varias_clases'
        if len(matches) == 1:
            class_path, row, files = next(iter(matches.values()))
            local = lambda value: datetime.fromisoformat(value).astimezone(ZoneInfo(context['timezone'])).date()
            if any(local(f['started_at_utc']) != local(row['started_at_utc']) for f in files):
                reason = 'fecha_archivo_difiere_de_fecha_clase_revisar'
            elif any(other['id'] != row['id'] and
                     any(t['path'] == s['path'] or (t.get('acquisition') == 'manual_vtt' and t['sha256'] == s['sha256'])
                         for t in other.get('transcripts', []) for s in source['transcripts'])
                     for _, other in classes):
                reason = 'asociacion_existente_con_otra_clase_revisar'
            else:
                for transcript in source['transcripts']:
                    original = inside(root, transcript['path'])
                    digest = hashlib.sha256(original.read_bytes()).hexdigest()
                    if digest != transcript['sha256']:
                        raise ValueError('Original VTT modificado; no asociar automaticamente')
                same = {t['sha256']: t for t in row.get('transcripts', [])}
                if all(s['sha256'] in same for s in source['transcripts']):
                    # Mismo contenido ya vinculado: la captura verifica su procedencia, no se duplica.
                    reports.append({'id': source['id'], 'status': 'duplicate', 'class_id': row['id']})
                    if not dry_run:
                        for s in source['transcripts']:
                            entry = same[s['sha256']]
                            entry.setdefault('recording_reference', reference)
                            proof = {'method': 'same_sha256_as_verified_viewer_source',
                                     'source_manifest_path': source_path.relative_to(root).as_posix()}
                            if proof not in entry.setdefault('verified_by', []):
                                entry['verified_by'].append(proof)
                        write_json(class_path, row)
                        source.update(status='duplicate', associated_class_id=row['id'],
                                      association_method='verified_viewer_reference_same_sha256',
                                      started_at_utc=row['started_at_utc'])
                        source.pop('association_pending_reason', None)
                        write_json(source_path, source)
                    continue
                reports.append({'id': source['id'], 'status': 'associated', 'class_id': row['id']})
                if not dry_run:
                    for transcript in source['transcripts']:
                        entry = {**transcript, 'recording_reference': reference,
                                 'processing_manifest_path': source_path.relative_to(root).as_posix(),
                                 'zoom_file_id_sha256': [f['file_id_sha256'] for f in files]}
                        row['transcripts'] = [t for t in row.get('transcripts', []) if t['id'] != entry['id']] + [entry]
                    write_json(class_path, row)
                    source.update(status='associated', associated_class_id=row['id'],
                                  association_method='verified_viewer_reference', started_at_utc=row['started_at_utc'])
                    source.pop('association_pending_reason', None)
                    write_json(source_path, source)
                continue
        reports.append({'id': source['id'], 'status': 'pending_date', 'reason': reason})
        if not dry_run:
            source['association_pending_reason'] = reason
            write_json(source_path, source)
    inventory_path = root / 'grabaciones.json'
    if not dry_run and inventory_path.exists():
        inventory = load_json(inventory_path)
        changed = False
        for entry in inventory:
            status = 'imported' if transcript_verified(context, entry['class_id']) else 'pending_import'
            if entry.get('transcript_status') != status:
                entry['transcript_status'] = status
                changed = True
        if changed:
            write_json(inventory_path, inventory)
    return reports


def store_unassigned(context, task, body, acquisition, index):
    """Guarda un VTT sin fecha; la asociacion posterior usa solo la referencia del visor."""
    cues = parse_vtt(body)
    folder = context['root'] / 'transcripciones-sin-fecha' / task['id']
    original = folder / 'fuentes' / 'original.vtt'
    path = folder / 'fuente.json'
    manifest = load_json(path, {'schema_version': 1, 'status': 'pending_date'})
    if task['reference'] and manifest.get('recording_reference') not in (None, task['reference']):
        raise ValueError('La referencia del VTT cambio; revisar la asociacion antes de importar')
    previous_hashes = {t['sha256'] for t in manifest.get('transcripts', [])}
    status, digest = archive_text(original, body)
    manifest.update(id=task['id'], course_id=context['course']['id'], acquisition=acquisition)
    if previous_hashes and digest not in previous_hashes:
        manifest['status'] = 'pending_date'
    if task['reference']:
        manifest['recording_reference'] = task['reference']
    manifest.setdefault('batch_position', index)
    manifest['transcripts'] = [{'id': task['id'], 'path': original.relative_to(context['root']).as_posix(),
        'sha256': digest, 'cue_count': len(cues), 'status': 'disponible', 'acquisition': acquisition}]
    write_json(path, manifest)
    return {'id': task['id'], 'status': status, 'cue_count': len(cues)}


def import_curls(context, text, dry_run=False):
    tasks = parse_curls(text)  # Validar todo antes de enviar la primera solicitud.
    if dry_run:
        return [{'id': t['id'], 'status': 'preview'} for t in tasks]
    reports = []
    with requests.Session() as session:
        for index, task in enumerate(tasks, 1):
            try:
                session.cookies.clear()
                with session.get(task['url'], headers=task['headers'], allow_redirects=False, timeout=(15, 45)) as response:
                    if response.status_code != 200:
                        reports.append({'id': task['id'], 'status': 'pending', 'http': response.status_code})
                        continue
                    body = response.content.decode('utf-8')
                reports.append(store_unassigned(context, task, body, 'provided_curl', index))
            except Exception as exc:
                # Los errores requests pueden incluir URL firmada: no persistir str(exc).
                reports.append({'id': task['id'], 'status': 'pending', 'error_type': type(exc).__name__})
    return reports


CAPTURE_SCHEMA = 'zoom-vtt-capture'


def load_capture(path):
    """Archivo generado en el navegador: VTT y hashes, sin URLs, cookies ni tokens."""
    data = json.loads(Path(path).read_text('utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema') != CAPTURE_SCHEMA or data.get('version') not in (1, 2):
        raise ValueError(f'{Path(path).name}: no es una captura {CAPTURE_SCHEMA} v1/v2')
    recording = None
    if data['version'] == 2:
        recording = validated_recording(data.get('recording'), Path(path).name)
    reference = data.get('recording_reference')
    if (not isinstance(data.get('id'), str) or not re.fullmatch(r'[0-9a-f]{20}', data['id'])
            or not isinstance(reference, dict) or reference.get('method') != 'observed_viewer_path_sha256'
            or not isinstance(reference.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])
            or not isinstance(data.get('vtt'), str)):
        raise ValueError(f'{Path(path).name}: captura incompleta o alterada')
    parse_vtt(data['vtt'])
    return {'id': data['id'], 'body': data['vtt'], 'recording': recording,
            'reference': {'method': reference['method'], 'sha256': reference['sha256']}}


def validated_recording(value, name):
    """Metadatos del reproductor (v2): hashes, inicio UTC, duracion y tema; nada mas."""
    hex64 = lambda v: isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v)
    try:
        started = datetime.fromisoformat(value['started_at_utc'].replace('Z', '+00:00'))
        ok = (hex64(value['occurrence_sha256']) and hex64(value['file_id_sha256'])
              and started.tzinfo is not None and value.get('file_type') in ('MP4', 'M4A')
              and type(value['duration_seconds']) is int and 0 < value['duration_seconds'] < 86400
              and isinstance(value['topic'], str) and len(value['topic']) <= 300)
    except (KeyError, TypeError, AttributeError, ValueError):
        ok = False
    if not ok:
        raise ValueError(f'{name}: metadatos de grabacion incompletos o alterados')
    return {'occurrence_sha256': value['occurrence_sha256'], 'file_id_sha256': value['file_id_sha256'],
            'file_type': value['file_type'], 'started_at_utc': started.astimezone(timezone.utc).isoformat(),
            'duration_seconds': value['duration_seconds'], 'topic': value['topic']}


def ensure_class(context, recording, reference):
    """Crea o completa la clase con la identidad de Zoom (hash de la ocurrencia).

    Es la misma identidad que usa la sincronizacion por listado, asi que no genera duplicados.
    """
    from knowledge_core import save_recording
    class_id = recording['occurrence_sha256'][:20]
    started = datetime.fromisoformat(recording['started_at_utc'])
    entry = {'file_id_sha256': recording['file_id_sha256'], 'file_type': recording['file_type'],
             'started_at_utc': started.isoformat(),
             'ended_at_utc': (started + timedelta(seconds=recording['duration_seconds'])).isoformat(),
             'recording_reference': reference}
    existing = next((load_json(p) for p in context['root'].glob('semanas/*/clases/*/clase.json')
                     if load_json(p, {}).get('id') == class_id), None)
    detail = (existing or {}).get('zoom_detail') or {'acquisition': 'zoom_player_info', 'source_timezone': 'UTC',
                                                     'occurrence_sha256': recording['occurrence_sha256'], 'files': []}
    if detail.get('occurrence_sha256') != recording['occurrence_sha256']:
        raise ValueError('La clase existente pertenece a otra ocurrencia de Zoom')
    files = [f for f in detail['files'] if f['file_id_sha256'] != entry['file_id_sha256']]
    known = next((f for f in detail['files'] if f['file_id_sha256'] == entry['file_id_sha256']), {})
    detail = {**detail, 'files': files + [{**entry, **{k: v for k, v in known.items() if k != 'recording_reference'}}]}
    save_recording(context, {'class_id': class_id,
                             'meeting_id': (existing or {}).get('meeting_id'),
                             'started_at_utc': (existing or {}).get('started_at_utc') or started.isoformat(),
                             'source_timezone': (existing or {}).get('source_timezone') or 'UTC',
                             'topic': (existing or {}).get('topic') or recording['topic'],
                             'zoom_detail': detail})
    inventory_path = context['root'] / 'grabaciones.json'
    inventory = load_json(inventory_path, [])
    row = next((r for r in inventory if r['class_id'] == class_id), None)
    if row is None:
        inventory.append({'class_id': class_id, 'meeting_id': None, 'started_at_utc': started.isoformat(),
                          'source_timezone': 'UTC', 'topic': recording['topic'],
                          'duration_minutes': round(recording['duration_seconds'] / 60),
                          'zoom_detail': detail, 'status': 'metadata_captured',
                          'transcript_status': 'pending_import', 'acquisition': 'zoom_player_info'})
    else:
        row.update(zoom_detail=detail, status='metadata_captured')
    write_json(inventory_path, inventory)
    return class_id


INBOX_MARKER = '.usil-zoom-capture-inbox'
MAX_CAPTURE_BYTES = 25 * 1024 * 1024


def capture_inbox(config):
    return Path(config['knowledge_root']).resolve() / 'entrada-capturas'


def init_capture_inbox(config):
    """Bandeja dedicada: el navegador solo obtiene acceso a esta carpeta."""
    inbox = capture_inbox(config)
    for folder in (inbox, inbox / 'importadas', inbox / 'rechazadas'):
        folder.mkdir(parents=True, exist_ok=True)
    marker = inbox / INBOX_MARKER
    if not marker.exists():
        marker.write_text('Bandeja de capturas zoom-vtt-capture. Elegir solo esta carpeta en el navegador.\n',
                          encoding='utf-8')
    return {'inbox': str(inbox), 'status': 'ready'}


def reference_index(config):
    """Referencias de visor verificadas por curso, a partir de los metadatos ya capturados."""
    index = {}
    for course_file in Path(config['knowledge_root']).resolve().glob('*/*/curso.json'):
        root = course_file.parent
        cid = load_json(course_file, {}).get('course_id')
        refs = set()
        for path in root.glob('semanas/*/clases/*/clase.json'):
            row = load_json(path, {})
            refs.update(f['recording_reference']['sha256'] for f in row.get('zoom_detail', {}).get('files', [])
                        if f.get('recording_reference'))
        for ref in refs:
            index.setdefault(ref, set()).add(cid)
    return index


def file_away(path, folder):
    """Mueve una captura procesada sin sobrescribir otra distinta con el mismo nombre."""
    target = folder / path.name
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest():
            path.unlink()
            return target
        target = folder / f'{path.stem}-{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}{path.suffix}'
    path.replace(target)
    return target


def import_inbox(config, dry_run=False):
    """Importa la bandeja sin indicar curso: cada captura va al curso cuyo visor coincide."""
    from knowledge_core import course_context, organize
    inbox = capture_inbox(config)
    if not (inbox / INBOX_MARKER).exists():
        raise ValueError('Bandeja sin inicializar; ejecuta conocimiento.py --init-capture-inbox')
    index = reference_index(config)
    names = {}
    library = Path(config['root']).resolve() / 'cursos.json' if config.get('root') else None
    for course in load_json(library, []) if library else []:
        if course.get('name'):
            names.setdefault(course['name'], set()).add(course['id'])
    reports, by_course = [], {}
    for path in sorted(inbox.glob('zoom-vtt-*.json')):
        try:
            if path.stat().st_size > MAX_CAPTURE_BYTES:
                raise ValueError('captura demasiado grande')
            capture = load_capture(path)
        except (ValueError, OSError) as exc:
            reports.append({'file': path.name, 'status': 'rejected', 'reason': str(exc)[:120]})
            if not dry_run:
                file_away(path, inbox / 'rechazadas')
            continue
        courses = index.get(capture['reference']['sha256'], set())
        if not courses and capture['recording']:
            # El tema de la reunion coincide exactamente con el nombre del curso en Canvas.
            courses = names.get(capture['recording']['topic'], set())
        if len(courses) != 1:
            # Sin metadatos de Zoom aun (o ambiguo): se conserva en la bandeja hasta sincronizar.
            reports.append({'file': path.name, 'id': capture['id'], 'status': 'waiting',
                            'reason': 'sin_metadatos_zoom' if not courses else 'visor_en_varios_cursos'})
            continue
        by_course.setdefault(next(iter(courses)), []).append((path, capture['id'], capture))
    for cid, items in sorted(by_course.items()):
        try:
            context = course_context(config, cid)
            if not dry_run:
                for _, _, capture in items:
                    if capture['recording']:
                        ensure_class(context, capture['recording'], capture['reference'])
            items = [(p, i) for p, i, _ in items]
            imported = {r['id']: r for r in import_captures(context, [str(p) for p, _ in items], dry_run)}
            linked = {a['id']: a['status'] for a in associate_pending(context, dry_run)}
            if not dry_run:
                organize(context)
            for path, capture_id in items:
                report = imported[capture_id]
                if not dry_run and report['status'] != 'pending':
                    file_away(path, inbox / 'importadas')
                reports.append({'file': path.name, 'course_id': cid, **report,
                                'association': linked.get(capture_id, 'sin_cambios')})
        except ValueError as exc:
            reports.extend({'file': p.name, 'course_id': cid, 'status': 'waiting', 'reason': str(exc)[:120]}
                           for p, *_ in items)
    return reports


def expand_paths(patterns):
    paths = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if glob.has_magic(pattern) else [pattern]
        if not matches:
            raise ValueError(f'Sin archivos para {pattern}')
        paths.extend(Path(p) for p in matches)
    return list(dict.fromkeys(paths))


def import_captures(context, patterns, dry_run=False):
    tasks = [load_capture(p) for p in expand_paths(patterns)]  # Validar el lote completo primero.
    bodies = {}
    for task in tasks:
        if bodies.setdefault(task['id'], task['body']) != task['body']:
            raise ValueError(f'Capturas distintas con el mismo id {task["id"]}; revisar antes de importar')
    tasks = list({t['id']: t for t in tasks}.values())
    if dry_run:
        return [{'id': t['id'], 'status': 'preview'} for t in tasks]
    reports = []
    for index, task in enumerate(tasks, 1):
        try:
            reports.append(store_unassigned(context, task, task['body'], 'browser_capture', index))
        except Exception as exc:
            reports.append({'id': task['id'], 'status': 'pending', 'error_type': type(exc).__name__})
    return reports

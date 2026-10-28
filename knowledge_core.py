"""Catalogo local de clases y fuentes; no modifica los materiales de Canvas."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import unicodedata
from zoneinfo import ZoneInfo

from canvas_sync import course_folder, write_json
from piloto import sha256


def slug(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-') or 'curso'


def load_json(path, default=None):
    return json.loads(path.read_text('utf-8-sig')) if path.exists() else default


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Ruta fuera de la biblioteca')
    return path


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + '.part')
    part.write_text(text, encoding='utf-8', newline='')
    os.replace(part, path)


def archive_text(path, text):
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if path.exists():
        previous = sha256(path)
        if previous == digest:
            return 'sin_cambios', digest
        history = path.parent / 'versiones' / f'{path.stem}-{previous}{path.suffix}'
        history.parent.mkdir(exist_ok=True)
        if history.exists():
            if sha256(history) != previous:
                raise ValueError('Colision de version')
        else:
            shutil.copy2(path, history)
    atomic_text(path, text)
    return 'guardado', digest


def week_tokens(text):
    return {f'S{int(n):02d}' for n in re.findall(r'\bS\s*(\d{1,2})\b', text or '', re.I)}


def millis(value):
    fields = value.replace(',', '.').split(':')
    if len(fields) not in (2, 3):
        raise ValueError('Marca de tiempo VTT invalida')
    seconds = float(fields[-1])
    minutes = int(fields[-2])
    hours = int(fields[0]) if len(fields) == 3 else 0
    if minutes >= 60 or seconds >= 60 or min(hours, minutes, seconds) < 0:
        raise ValueError('Marca de tiempo VTT invalida')
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def timestamp(value):
    seconds, ms = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}'


def parse_vtt(text):
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if not re.match(r'^WEBVTT(?:\s|$)', text):
        raise ValueError('La respuesta no es WEBVTT')
    cues = []
    timing = re.compile(r'^(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})(?:\s.*)?$')
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        if not lines or lines[0].startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')):
            continue
        offset = 0 if '-->' in lines[0] else 1
        if len(lines) <= offset or not (match := timing.match(lines[offset].strip())):
            raise ValueError('Bloque de transcripcion invalido')
        start, end = (millis(v) for v in match.groups())
        if end < start:
            raise ValueError('Intervalo VTT invertido')
        body = '\n'.join(lines[offset+1:])
        body = re.sub(r'<v\s+([^>]+)>', r'\1: ', body)
        body = html.unescape(re.sub(r'<[^>]+>', '', body)).strip()
        cues.append({'id': len(cues)+1, 'start_ms': start, 'end_ms': end,
                     'start': timestamp(start), 'end': timestamp(end), 'text': body})
    if not cues:
        raise ValueError('Transcripcion sin segmentos')
    return cues


def course_context(config, cid):
    library = Path(config['root']).resolve()
    courses = load_json(library / 'cursos.json', [])
    course = next((c for c in courses if c['id'] == cid), None)
    if course is None or not course.get('name'):
        raise ValueError('Curso ausente del inventario local; ejecuta canvas_sync.py --list-courses')
    period_match = re.search(r'20\d\d-\d\d', course['name'])
    period = period_match.group() if period_match else config.get('current_period') or 'sin-periodo'
    if config.get('current_period') and period != config['current_period']:
        raise ValueError('El curso no pertenece al periodo actual configurado')
    source = course_folder(library, course)
    records = load_json(source / 'recursos.json', [])
    name = re.sub(r'^\d+\s*-\s*', '', course['name']).split(' - 20')[0]
    root = Path(config['knowledge_root']).resolve() / period / f'{slug(name)}-{cid}'
    manual = load_json(root / 'relaciones-manuales.json', {'pdf_weeks': {}, 'classes': {}, 'calendar': []})
    return {'root': root, 'library': library, 'source': source, 'course': course,
            'records': records, 'manual': manual, 'timezone': config.get('knowledge_timezone', 'America/Lima')}


def material_catalog(context):
    materials, issues = [], []
    for record in context['records']:
        relative = record.get('path')
        if not ((relative and Path(relative).suffix.lower() == '.pdf') or record['title'].lower().endswith('.pdf')):
            continue
        source = inside(context['library'], relative) if relative else None
        folder_weeks = week_tokens(record.get('week'))
        title_weeks = week_tokens(record['title'])
        weeks = folder_weeks | title_weeks
        manual_week = context['manual'].get('pdf_weeks', {}).get(str(record['id']))
        if manual_week and not re.fullmatch(r'S\d{2}', manual_week):
            raise ValueError('Semana manual invalida')
        week = manual_week or (next(iter(weeks)) if len(weeks) == 1 else None)
        evidence = {'folder_weeks': sorted(folder_weeks), 'title_weeks': sorted(title_weeks),
                    'manual': bool(manual_week)}
        exists = source is not None and source.is_file()
        material = {'id': f'canvas:{context["course"]["id"]}:{record["id"]}',
                    'canvas_item_id': record['id'], 'title': record['title'], 'week': week,
                    'module': record.get('module'), 'evidence': evidence,
                    'library_path': Path(relative).as_posix() if relative else None,
                    'sha256': sha256(source) if exists else None,
                    'status': 'disponible' if exists else 'pdf_pendiente'}
        materials.append(material)
        if len(weeks) > 1 and not manual_week:
            issues.append(f'PDF {record["id"]}: conflicto de semanas {", ".join(sorted(weeks))}; requiere revision.')
        elif not week:
            issues.append(f'PDF {record["id"]}: recurso general o semana sin determinar.')
        if not exists:
            issues.append(f'PDF {record["id"]}: archivo aun no descargado.')
    return materials, issues


def calendar(context):
    manual = context['manual'].get('calendar')
    if manual:
        ranges = []
        for row in manual:
            start, end = date.fromisoformat(row['start']), date.fromisoformat(row['end'])
            if end < start or not re.fullmatch(r'S\d{2}', row['week']):
                raise ValueError('Intervalo manual invalido')
            ranges.append({**row, 'source': 'manual'})
        if len({r['week'] for r in ranges}) != len(ranges):
            raise ValueError('Semana repetida en calendario manual')
        return ranges
    weeks = defaultdict(set)
    for record in context['records']:
        unit = re.match(r'(\d{1,2})\s', record.get('module') or '')
        if unit:
            weeks[int(unit[1])].update(week_tokens(record.get('week')))
    ranges = []
    for record in context['records']:
        unit = re.search(r'Unidad\s*(\d+)', record.get('module') or '', re.I)
        dates = re.findall(r'\b(\d{2}/\d{2}/\d{2,4})\b', record['title'])
        if not unit or len(dates) != 2:
            continue
        parsed = [datetime.strptime(v, '%d/%m/%y' if len(v) == 8 else '%d/%m/%Y').date() for v in dates]
        labels = sorted(weeks[int(unit[1])])
        days = (parsed[1] - parsed[0]).days + 1
        if not labels or days <= 0 or (days + 6) // 7 != len(labels):
            continue
        for index, week in enumerate(labels):
            start = parsed[0] + timedelta(days=7*index)
            end = min(parsed[1], start+timedelta(days=6))
            ranges.append({'week': week, 'start': start.isoformat(), 'end': end.isoformat(),
                           'source': f'canvas:{record["id"]}', 'unit': int(unit[1])})
    return ranges


def week_for_date(value, ranges):
    matches = {r['week'] for r in ranges if r['start'] <= value <= r['end']}
    return next(iter(matches)) if len(matches) == 1 else None


def recording_identity(cid, meeting_id, started_at, occurrence_id=None):
    value = occurrence_id or f'{cid}|{meeting_id}|{started_at}'
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]


def class_location(context, recording):
    if not re.fullmatch(r'[a-f0-9]{20}', recording['class_id']):
        raise ValueError('ID de clase invalido')
    # Reusa la carpeta existente aunque Canvas publique luego un calendario nuevo.
    for path in context['root'].glob('semanas/*/clases/*/clase.json'):
        if load_json(path, {}).get('id') == recording['class_id']:
            return path.parent
    instant = datetime.fromisoformat(recording['started_at_utc'].replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('La fecha de grabacion requiere zona horaria verificada')
    local = instant.astimezone(ZoneInfo(context['timezone']))
    week = week_for_date(local.date().isoformat(), calendar(context)) or 'sin-semana'
    return context['root'] / 'semanas' / week / 'clases' / f'{local:%Y-%m-%d_%H%M}_{recording["class_id"]}'


def save_recording(context, recording):
    """Crea tambien las clases cuya transcripcion sigue pendiente."""
    folder = class_location(context, recording)
    index = folder / 'clase.json'
    existing = load_json(index, {'id': recording['class_id'], 'transcripts': []})
    instant = datetime.fromisoformat(recording['started_at_utc'].replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('La fecha de grabacion requiere zona horaria verificada')
    local = instant.astimezone(ZoneInfo(context['timezone']))
    existing.update(course_id=context['course']['id'], meeting_id=recording['meeting_id'],
                    started_at_utc=instant.astimezone(timezone.utc).isoformat(),
                    started_at_local=local.isoformat(), source_timezone=recording.get('source_timezone'),
                    topic=recording.get('topic'), week=week_for_date(local.date().isoformat(), calendar(context)))
    existing['started_at_local'] = local.isoformat()
    if recording.get('zoom_detail'):
        existing['zoom_detail'] = recording['zoom_detail']
    (folder / 'fuentes').mkdir(parents=True, exist_ok=True)
    (folder / 'procesado').mkdir(exist_ok=True)
    write_json(index, existing)
    return folder, existing


def save_transcript(context, recording, body):
    cues = parse_vtt(body)
    folder, existing = save_recording(context, recording)
    transcript_id = recording.get('transcript_id') or recording['class_id']
    transcript_id = hashlib.sha256(transcript_id.encode()).hexdigest()[:20]
    path = folder / 'fuentes' / f'zoom-{transcript_id}.vtt'
    status, digest = archive_text(path, body)
    entry = {'id': transcript_id, 'path': path.relative_to(context['root']).as_posix(),
             'sha256': digest, 'cue_count': len(cues), 'status': 'disponible',
             'acquisition': recording.get('acquisition', 'zoom_observed_request')}
    if recording.get('recording_reference'):
        entry['recording_reference'] = recording['recording_reference']
        entry['zoom_file_id_sha256'] = recording.get('zoom_file_id_sha256', [])
    existing['transcripts'] = [t for t in existing['transcripts'] if t['id'] != transcript_id] + [entry]
    write_json(folder / 'clase.json', existing)
    return {'class_id': existing['id'], 'status': status, 'cue_count': len(cues), 'sha256': digest}


def transcript_verified(context, class_id):
    for index in context['root'].glob('semanas/*/clases/*/clase.json'):
        row = load_json(index, {})
        if row.get('id') == class_id and row.get('transcripts'):
            return all(inside(context['root'], t['path']).is_file()
                       and sha256(inside(context['root'], t['path'])) == t['sha256']
                       for t in row['transcripts'])
    return False


def organize(context, dry_run=False):
    root = context['root']
    materials, issues = material_catalog(context)
    ranges = calendar(context)
    classes = []
    material_ids = {m['id'] for m in materials}
    for path in sorted(root.glob('semanas/*/clases/*/clase.json')):
        row = load_json(path, {})
        local = datetime.fromisoformat(row['started_at_utc'].replace('Z', '+00:00')).astimezone(ZoneInfo(context['timezone']))
        row['started_at_local'] = local.isoformat()
        row['week'] = week_for_date(local.date().isoformat(), ranges)
        row['shared_week_materials'] = [m['id'] for m in materials if row['week'] and m['week'] == row['week']]
        manual_ids = context['manual'].get('classes', {}).get(row['id'], {}).get('pdf_ids', [])
        if not set(manual_ids).issubset(material_ids):
            raise ValueError('Relacion manual referencia un PDF desconocido')
        row['materials'] = manual_ids
        row['manifest_path'] = path.relative_to(root).as_posix()
        classes.append(row)
        if not row['week']:
            issues.append(f'Clase {row["id"]}: fecha fuera del calendario publicado.')
        if not row.get('transcripts'):
            issues.append(f'Clase {row["id"]}: transcripcion pendiente; admite importacion manual.')
        for transcript in row.get('transcripts', []):
            original = inside(root, transcript['path'])
            if not original.is_file() or sha256(original) != transcript['sha256']:
                issues.append(f'Clase {row["id"]}: original VTT ausente o modificado; reimportar antes de procesar.')
        if not row['materials'] and row['shared_week_materials']:
            issues.append(f'Clase {row["id"]}: materiales semanales disponibles; uso en clase sin confirmar.')
        if not dry_run:
            write_json(path, row)
    starts = {}
    for row in classes:
        starts.setdefault((row['meeting_id'], row['started_at_utc']), []).append(row['id'])
    for identifiers in starts.values():
        if len(identifiers) > 1:
            issues.append(f'Clases {", ".join(identifiers)}: misma reunion e inicio; posible duplicado para revision.')
    weeks = sorted({r['week'] for r in ranges} | {m['week'] for m in materials if m['week']} | {c['week'] for c in classes if c['week']})
    unassigned = []
    for path in sorted(root.glob('transcripciones-sin-fecha/*/fuente.json')):
        row = load_json(path, {})
        if row.get('status') != 'pending_date':
            continue
        row['manifest_path'] = path.relative_to(root).as_posix()
        unassigned.append(row)
        issues.append(f'Transcripcion {row["id"]}: fecha de clase pendiente de verificar.')
        reasons = {'visor_coincide_con_varias_clases': 'El visor coincide con varias clases.',
                   'fecha_archivo_difiere_de_fecha_clase_revisar': 'La fecha del archivo difiere de la fecha de la clase.',
                   'asociacion_existente_con_otra_clase_revisar': 'Existe una asociacion con otra clase; se conserva para revision.'}
        reason = reasons.get(row.get('association_pending_reason'))
        if reason:
            issues.append(f'Transcripcion {row["id"]}: {reason}')
    if dry_run:
        return {'course_id': context['course']['id'], 'pdfs': len(materials), 'classes': len(classes),
                'weeks': weeks, 'unassigned_transcripts': len(unassigned), 'pending': issues, 'destination': str(root)}
    write_json(root / 'curso.json', {'schema_version': 1, 'course_id': context['course']['id'],
               'name': context['course']['name'], 'timezone': context['timezone'],
               'library_relative_root': Path(os.path.relpath(context['library'], root)).as_posix()})
    write_json(root / 'calendario.json', ranges)
    write_json(root / 'materiales.json', materials)
    if not (root / 'relaciones-manuales.json').exists():
        write_json(root / 'relaciones-manuales.json', context['manual'])
    lines = [f'# {context["course"]["name"]}', '', 'Los PDFs semanales son material compartido; su uso en una clase requiere confirmacion.', '']
    for week in [*weeks, 'sin-semana']:
        weekly = [m for m in materials if (m['week'] or 'sin-semana') == week]
        sessions = [c for c in classes if (c['week'] or 'sin-semana') == week]
        if not weekly and not sessions and week == 'sin-semana':
            continue
        write_json(root / 'semanas' / week / 'materiales.json', {'week': week, 'pdfs': weekly,
                   'classes': [{'id': c['id'], 'path': c['manifest_path']} for c in sessions]})
        (root / 'semanas' / week / 'clases').mkdir(exist_ok=True)
        lines.extend([f'## {week}', ''])
        for m in weekly:
            target = Path(os.path.relpath(inside(context['library'], m['library_path']), root)).as_posix() if m['library_path'] else None
            title = m['title'].replace('[','\\[').replace(']','\\]')
            lines.append(f'- [{title}](<{target}>)' if target else f'- {title}: pendiente de descarga')
        for c in sessions:
            lines.append(f'- Clase [{c["started_at_local"]}](<{c["manifest_path"]}>)')
            for t in c.get('transcripts', []):
                lines.append(f'  - [Transcripcion original ({t["cue_count"]} segmentos)](<{t["path"]}>)')
        lines.append('')
    if unassigned:
        lines.extend(['## Transcripciones pendientes de fecha', '',
                      'Originales verificados; no asignar semanas hasta confirmar la grabacion.', ''])
        for row in sorted(unassigned, key=lambda r: (r.get('batch_position', 9999), r['id'])):
            label = f'VTT {row["batch_position"]}' if row.get('batch_position') else row['id']
            for t in row['transcripts']:
                lines.append(f'- [{label}: {t["cue_count"]} segmentos](<{t["path"]}>)')
        lines.append('')
    processed = []
    material_hashes = {m['id']: m['sha256'] for m in materials}
    for path in sorted((root / 'procesado' / 'pdfs').glob('*/**/estado.json')):
        state = load_json(path, {})
        if state.get('processed_pages', 0) and state.get('source_id') in material_hashes and state.get('source_sha256') == material_hashes[state['source_id']]:
            note = path.parent / 'interpretacion.md'
            if note.exists():
                processed.append((f'PDF {state["source_id"]}: {state["processed_pages"]}/{state["expected_pages"]} paginas, {state["status"]}, {state["model"]}', note))
    for row in [*classes, *unassigned]:
        folder = inside(root, row['manifest_path']).parent
        folders = {folder} | {inside(root, t['processing_manifest_path']).parent
                              for t in row.get('transcripts', []) if t.get('processing_manifest_path')}
        sources = {t['path']: t['sha256'] for t in row.get('transcripts', [])}
        paths = {path for base in folders for path in (base / 'procesado' / 'transcripciones').glob('*/**/estado.json')}
        for path in sorted(paths):
            state = load_json(path, {})
            if state.get('processed_segments', 0) and state.get('source_path') in sources and state.get('source_sha256') == sources[state['source_path']]:
                note = path.parent / 'corregida.md'
                if note.exists():
                    description = f'Clase {row["started_at_local"]}' if row.get('started_at_local') else f'VTT {row.get("batch_position", row["id"])} (fecha pendiente)'
                    label = f'{description}: {state["processed_segments"]}/{state["expected_segments"]} segmentos, {state["status"]}, {state["model"]}'
                    processed.append((label, note))
                    study = path.parent / 'estudio.md'
                    if study.exists():
                        processed.append((f'Seleccion academica de {description} ({state["status"]})', study))
    if processed:
        lines.extend(['## Material procesado', ''])
        lines.extend(f'- [{label}](<{note.relative_to(root).as_posix()}>)' for label, note in processed)
        lines.append('')
    atomic_text(root / 'indice.md', '\n'.join(lines))
    (root / 'procesado' / 'pdfs').mkdir(parents=True, exist_ok=True)
    (root / 'entrada-transcripciones').mkdir(exist_ok=True)
    atomic_text(root / 'pendientes.md', '# Relaciones y fuentes pendientes\n\n' + '\n'.join(f'- {v}' for v in issues) + '\n')
    return {'course_id': context['course']['id'], 'pdfs': len(materials), 'classes': len(classes),
            'weeks': weeks, 'unassigned_transcripts': len(unassigned), 'pending_count': len(issues), 'destination': str(root)}

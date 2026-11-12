"""Importacion reproducible de VTT exportados manualmente, sin navegador."""
from datetime import datetime, timezone
from pathlib import Path

from canvas_sync import write_json
from knowledge_core import inside, load_json, parse_vtt, recording_identity, save_transcript
from piloto import sha256


def manual_recording(context, started_at=None, class_id=None, meeting_id=None, topic=None,
                     transcript_key='principal'):
    if class_id:
        matches = [load_json(p, {}) for p in context['root'].glob('semanas/*/clases/*/clase.json')]
        existing = next((row for row in matches if row.get('id') == class_id), None)
        if existing is None:
            raise ValueError('ID de clase desconocido; consulta indice.md o grabaciones.json')
        recording = {key: existing.get(key) for key in
                     ('meeting_id', 'started_at_utc', 'source_timezone', 'topic')}
        recording['class_id'] = class_id
        if started_at or meeting_id:
            raise ValueError('Con class_id usa la fecha y reunion de la clase existente')
    else:
        if not started_at:
            raise ValueError('Indica class_id existente o started_at con zona horaria')
        instant = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        if instant.tzinfo is None:
            raise ValueError('started_at requiere offset, por ejemplo 2026-10-07T19:03:53-05:00')
        utc = instant.astimezone(timezone.utc).isoformat()
        meeting = str(meeting_id or 'manual')
        matches = []
        if meeting_id:
            for path in context['root'].glob('semanas/*/clases/*/clase.json'):
                row = load_json(path, {})
                known = datetime.fromisoformat(row['started_at_utc'].replace('Z', '+00:00'))
                if known == instant and row.get('meeting_id') == meeting:
                    matches.append(row)
        if len(matches) > 1:
            raise ValueError('Varias clases coinciden; indica class_id explicitamente')
        recording = {'class_id': recording_identity(context['course']['id'], meeting, utc),
                     'meeting_id': meeting, 'started_at_utc': utc,
                     'source_timezone': str(instant.tzinfo), 'topic': topic or context['course']['name']}
        if matches:
            recording.update({key: matches[0].get(key) for key in
                              ('id', 'source_timezone', 'topic')})
            recording['class_id'] = recording.pop('id')
    if topic:
        recording['topic'] = topic
    # Clave estable dentro de una clase: cambiar el contenido conserva su version anterior.
    recording['transcript_id'] = 'manual|' + recording['class_id'] + '|' + transcript_key
    recording['acquisition'] = 'manual_vtt'
    return recording


def import_vtt(context, path, *, started_at=None, class_id=None, meeting_id=None,
               topic=None, transcript_key='principal', dry_run=False):
    path = Path(path).resolve()
    # No normalizar saltos de linea ni BOM: el original importado conserva sus bytes UTF-8.
    body = path.read_bytes().decode('utf-8')
    cues = parse_vtt(body)
    recording = manual_recording(context, started_at, class_id, meeting_id, topic, transcript_key)
    if dry_run:
        return {'class_id': recording['class_id'], 'cue_count': len(cues), 'status': 'preview'}
    return save_transcript(context, recording, body)


def merge_classes(context, source_id, target_id, dry_run=False):
    """Unifica una clase manual con la clase inventariada por Zoom de la misma sesion.

    Los originales y resultados procesados conservan sus rutas: la clase de origen se
    retira como clase.unificada.json y sus transcripciones apuntan a esa carpeta.
    """
    if source_id == target_id:
        raise ValueError('Origen y destino deben ser clases distintas')
    root = context['root']
    found = {}
    for path in root.glob('semanas/*/clases/*/clase.json'):
        row = load_json(path, {})
        if row.get('id') in (source_id, target_id):
            found[row['id']] = (path, row)
    if source_id not in found or target_id not in found:
        raise ValueError('ID de clase desconocido; consulta indice.md o pendientes.md')
    (source_path, source), (target_path, target) = found[source_id], found[target_id]
    if source.get('zoom_detail'):
        raise ValueError('La clase de origen tiene metadatos de Zoom; solo se unifican clases manuales')
    if (source.get('meeting_id'), source.get('started_at_utc')) != (target.get('meeting_id'), target.get('started_at_utc')):
        raise ValueError('Las clases no comparten reunion e inicio; revisar manualmente')
    target_ids = {t['id']: t for t in target.get('transcripts', [])}
    moved = []
    for transcript in source.get('transcripts', []):
        original = inside(root, transcript['path'])
        if not original.is_file() or sha256(original) != transcript['sha256']:
            raise ValueError('Original VTT ausente o modificado; no unificar')
        if transcript['id'] in target_ids and target_ids[transcript['id']]['sha256'] != transcript['sha256']:
            raise ValueError('La clase destino tiene otra version de la misma transcripcion')
        moved.append(transcript)
    retired = source_path.with_name('clase.unificada.json')
    report = {'source': source_id, 'target': target_id, 'transcripts': len(moved), 'status': 'preview' if dry_run else 'merged'}
    if dry_run:
        return report
    for transcript in moved:
        entry = dict(transcript)
        # agent_tasks ubica los resultados por la carpeta de este manifest: se reusan.
        entry.setdefault('processing_manifest_path', retired.relative_to(root).as_posix())
        entry['merged_from_class_id'] = source_id
        target_ids[entry['id']] = entry
    target['transcripts'] = list(target_ids.values())
    relations_path = root / 'relaciones-manuales.json'
    relations = load_json(relations_path, context['manual'])
    classes = relations.setdefault('classes', {})
    if source_id in classes:
        pdf_ids = classes.setdefault(target_id, {}).setdefault('pdf_ids', [])
        pdf_ids.extend(p for p in classes.pop(source_id).get('pdf_ids', []) if p not in pdf_ids)
        write_json(relations_path, relations)
        context['manual'] = relations
    write_json(target_path, target)
    write_json(retired, {**source, 'merged_into_class_id': target_id,
                         'merged_at': datetime.now(timezone.utc).isoformat()})
    source_path.unlink()
    return report


def import_manifest(context, path, dry_run=False):
    path = Path(path).resolve()
    data = load_json(path, {})
    rows = data.get('transcripts')
    if not isinstance(rows, list) or not rows:
        raise ValueError('El manifest requiere una lista transcripts no vacia')
    tasks = []
    identities = set()
    allowed = {'path', 'started_at', 'class_id', 'meeting_id', 'topic', 'transcript_key'}
    for row in rows:
        if not isinstance(row, dict) or set(row) - allowed or not row.get('path'):
            raise ValueError('Entrada de importacion invalida')
        options = {k: v for k, v in row.items() if k != 'path' and v is not None}
        source = path.parent / row['path']
        # Validar todo el lote antes de escribir la primera clase.
        preview = import_vtt(context, source, dry_run=True, **options)
        identity = (preview['class_id'], options.get('transcript_key', 'principal'))
        if identity in identities:
            raise ValueError('Transcripcion repetida en el lote; usa otra transcript_key para una segunda parte')
        identities.add(identity)
        tasks.append((source, options, preview))
    if dry_run:
        return [preview for _, _, preview in tasks]
    return [import_vtt(context, source, **options) for source, options, _ in tasks]

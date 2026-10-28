"""Importacion reproducible de VTT exportados manualmente, sin navegador."""
from datetime import datetime, timezone
from pathlib import Path

from knowledge_core import load_json, parse_vtt, recording_identity, save_transcript


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

"""Procesa archivos individuales con agy y Gemini Flash High."""
import argparse
import json
from agent_tasks import process_pdf, process_transcript
from canvas_sync import write_json
from knowledge_core import course_context, load_json, material_catalog, organize
from settings import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=int, required=True)
    parser.add_argument('--kind', choices=('pdf', 'transcript'), required=True)
    parser.add_argument('--config')
    parser.add_argument('--model', help='Prevalece sobre agents.model')
    parser.add_argument('--source-id', help='ID de recurso PDF o transcripcion puntual')
    parser.add_argument('--limit', type=int, help='Cantidad maxima de archivos')
    parser.add_argument('--page-limit', type=int, help='Piloto visual parcial; el PDF queda marcado partial')
    parser.add_argument('--block-limit', type=int, help='Piloto parcial de VTT; conserva estado partial')
    parser.add_argument('--unassigned', action='store_true', help='Solo VTT pendientes de fecha')
    parser.add_argument('--workers', type=int, help='Tareas agy concurrentes por transcripcion (1-8)')
    parser.add_argument('--chunk-size', type=int, help='Segmentos por tarea de transcripcion')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if any(v is not None and v < 1 for v in (args.limit, args.page_limit, args.block_limit, args.workers, args.chunk_size)):
        parser.error('Los limites deben ser positivos')
    if args.page_limit and args.kind != 'pdf':
        parser.error('--page-limit solo se usa para PDFs')
    if args.block_limit and args.kind != 'transcript':
        parser.error('--block-limit solo se usa para transcripciones')
    if (args.unassigned or args.workers or args.chunk_size) and args.kind != 'transcript':
        parser.error('--unassigned, --workers y --chunk-size solo se usan para transcripciones')
    if args.workers and args.workers > 8:
        parser.error('--workers no puede superar 8')
    settings = load_settings(args.config)
    context = course_context(settings, args.course)
    config = dict(settings['agents'])
    if args.model:
        config['model'] = args.model
    if args.workers:
        config['workers'] = args.workers
    if args.chunk_size:
        config['transcript_chunk_size'] = args.chunk_size
    tasks = []
    if args.kind == 'pdf':
        materials, _ = material_catalog(context)
        tasks = [(str(m['canvas_item_id']), m) for m in materials
                 if m['status'] == 'disponible' and not m.get('duplicate_of')]
    else:
        paths = [] if args.unassigned else sorted(context['root'].glob('semanas/*/clases/*/clase.json'))
        pending_paths = sorted(context['root'].glob('transcripciones-sin-fecha/*/fuente.json'),
                               key=lambda p: sum(t['cue_count'] for t in load_json(p, {}).get('transcripts', [])))
        paths.extend(p for p in pending_paths if load_json(p, {}).get('status') == 'pending_date')
        for path in paths:
            manifest = load_json(path, {})
            manifest['manifest_path'] = path.relative_to(context['root']).as_posix()
            tasks.extend((t['id'], (manifest, t)) for t in manifest.get('transcripts', []))
    if args.source_id:
        tasks = [task for task in tasks if task[0] == args.source_id]
    if args.limit:
        tasks = tasks[:args.limit]
    if args.dry_run:
        print(json.dumps({'kind': args.kind, 'model': config['model'], 'sources': [t[0] for t in tasks]}, indent=2))
        return
    if not tasks:
        raise ValueError('No se encontraron fuentes para procesar')
    results = []
    for identifier, data in tasks:
        try:
            report = process_pdf(context, data, config, args.force, args.page_limit) if args.kind == 'pdf' else process_transcript(context, *data, config, args.force, args.block_limit)
            results.append({'id': identifier, **report})
        except Exception as exc:
            results.append({'id': identifier, 'status': 'pending', 'error_type': type(exc).__name__})
    write_json(context['root'] / f'ultima-interpretacion-{args.kind}.json', results)
    organize(context)
    print(json.dumps(results, ensure_ascii=True, indent=2))
    if any(r['status'] != 'complete' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Proceso incompleto: {type(exc).__name__}: {exc}' if isinstance(exc, ValueError)
              else f'Proceso incompleto: {type(exc).__name__}', flush=True)
        raise SystemExit(1)

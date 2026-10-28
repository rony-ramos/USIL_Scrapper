"""Organiza PDFs existentes y transcripciones observadas en Zoom."""
import argparse
import json
from pathlib import Path
import sys
from knowledge_core import course_context, organize
from settings import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=int, required=True)
    parser.add_argument('--config')
    parser.add_argument('--sync-zoom', action='store_true', help='Capturar solo metadatos; no abrir reproductores Zoom')
    manual = parser.add_mutually_exclusive_group()
    manual.add_argument('--import-vtt', help='Archivo VTT UTF-8 exportado manualmente')
    manual.add_argument('--import-manifest', help='Lote JSON; rutas relativas a ese JSON')
    manual.add_argument('--import-curl', metavar='ARCHIVO_O_-', help='cURL VTT proporcionados; - lee stdin sin guardar credenciales')
    parser.add_argument('--class-date', help='Fecha ISO con offset para crear una clase manual')
    parser.add_argument('--class-id', help='Asociar VTT a una clase ya inventariada')
    parser.add_argument('--meeting-id', help='ID de reunion opcional para una clase manual nueva')
    parser.add_argument('--transcript-key', default='principal', help='Clave estable dentro de la clase')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Volver a capturar metadatos de grabaciones conocidas')
    parser.add_argument('--limit', type=int, help='Limitar capturas para un piloto; descubre todo el listado')
    parser.add_argument('--login-timeout', type=int, default=120)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit debe ser positivo')
    if (args.class_date or args.class_id or args.meeting_id) and not args.import_vtt:
        parser.error('--class-date, --class-id y --meeting-id requieren --import-vtt')
    if args.sync_zoom and (args.import_vtt or args.import_manifest or args.import_curl):
        parser.error('Ejecuta la sincronizacion y la importacion en comandos separados')
    config = load_settings(args.config)
    context = course_context(config, args.course)
    result = organize(context, dry_run=args.dry_run)
    if args.import_vtt or args.import_manifest:
        from manual_transcripts import import_manifest, import_vtt
        if args.import_manifest:
            result['imports'] = import_manifest(context, args.import_manifest, args.dry_run)
        else:
            result['imports'] = [import_vtt(context, args.import_vtt,
                started_at=args.class_date, class_id=args.class_id, meeting_id=args.meeting_id,
                transcript_key=args.transcript_key, dry_run=args.dry_run)]
        result.update(organize(context, dry_run=args.dry_run))
    if args.import_curl:
        from transcript_requests import associate_pending, import_curls
        text = sys.stdin.read() if args.import_curl == '-' else Path(args.import_curl).read_text('utf-8-sig')
        result['imports'] = import_curls(context, text, args.dry_run)
        del text
        result['associations'] = associate_pending(context, args.dry_run)
        result.update(organize(context, dry_run=args.dry_run))
    if args.sync_zoom:
        from zoom_capture import sync_zoom
        result['zoom'] = sync_zoom(config, context, dry_run=args.dry_run, refresh=args.refresh,
                                   limit=args.limit, login_timeout=args.login_timeout)
        if not args.dry_run:
            from canvas_sync import write_json
            write_json(context['root'] / 'ultima-sincronizacion-zoom.json', result['zoom'])
            from transcript_requests import associate_pending
            result['associations'] = associate_pending(context)
        result.update(organize(context, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if result.get('zoom', {}).get('pending', 0) or any(r['status'] == 'pending' for r in result.get('imports', [])):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Proceso incompleto: {type(exc).__name__}: {exc}' if isinstance(exc, ValueError)
              else f'Proceso incompleto: {type(exc).__name__}', flush=True)
        raise SystemExit(1)

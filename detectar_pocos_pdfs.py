"""Monitoreo y diagnostico de descargas: detecta cursos con pocos PDFs y explica la causa."""
import argparse
import json
from pathlib import Path
import re
from collections import Counter
from settings import load_settings


def count_physical_files(course_dir):
    """Cuenta archivos reales en disco evitando .part y carpetas de versiones."""
    pdfs = 0
    pages = 0
    assignments = 0
    if not course_dir.exists():
        return 0, 0, 0
    for p in course_dir.rglob('*'):
        if p.is_file():
            if 'versiones' in p.parts:
                continue
            name_lower = p.name.lower()
            if name_lower.endswith('.pdf') and not name_lower.endswith('.part'):
                pdfs += 1
            elif name_lower.endswith('-pagina.json'):
                pages += 1
            elif name_lower.endswith('-tarea.json'):
                assignments += 1
    return pdfs, pages, assignments


def analyze_course(cid, course_info, root, umbral, folder=None):
    name = course_info.get('name', f'Curso {cid}')
    period_match = re.search(r'20\d\d-\d\d', name)
    period = period_match.group() if period_match else 'Sin periodo'

    if not folder or not folder.exists():
        return {
            'id': cid,
            'name': name,
            'period': period,
            'folder': None,
            'status': 'no_iniciado',
            'pdfs': 0,
            'pages': 0,
            'assignments': 0,
            'total_resources': 0,
            'counts': {},
            'pocos_pdfs': True,
            'diagnosis': 'No iniciado (pendiente de primera sincronización)'
        }

    pdfs, pages, assignments = count_physical_files(folder)
    recursos_path = folder / 'recursos.json'
    counts = {}
    total_resources = 0
    if recursos_path.exists():
        try:
            records = json.loads(recursos_path.read_text('utf-8'))
            total_resources = len(records)
            counts = dict(Counter(r.get('status', 'desconocido') for r in records))
        except Exception:
            pass

    pocos = pdfs <= umbral

    # Diagnosticar causa principal si tiene pocos PDFs
    sin_acceso = counts.get('sin_acceso_sharepoint', 0)
    no_encontrado = counts.get('no_encontrado_sharepoint', 0)
    pend_conector = counts.get('pendiente_conector', 0)
    pendiente = counts.get('pendiente', 0)
    incompleto = counts.get('enlace_incompleto', 0)
    guardado = counts.get('contenido_guardado', 0)

    if not pocos:
        diagnosis = f'Saludable ({pdfs} PDFs descargados)'
    elif sin_acceso > 0 and sin_acceso >= max(no_encontrado, pendiente, 1):
        diagnosis = f'Bloqueado por SharePoint: {sin_acceso} archivos sin acceso'
    elif no_encontrado > 0 and no_encontrado >= max(sin_acceso, pendiente, 1):
        diagnosis = f'Archivos eliminados o rotos: {no_encontrado} enlaces 404'
    elif pend_conector > 0:
        diagnosis = f'Enlaces externos/Office365 sin conector: {pend_conector} enlaces'
    elif pendiente > 0:
        diagnosis = f'Descarga incompleta o interrumpida: {pendiente} recursos pendientes'
    elif incompleto > 0:
        diagnosis = f'Enlaces incompletos/relativos: {incompleto} enlaces'
    elif guardado > 0 and pdfs == 0:
        diagnosis = f'Curso digital sin PDFs ({guardado} páginas/tareas Canvas)'
    elif total_resources == 0:
        diagnosis = 'Sin módulos o recursos publicados en Canvas'
    else:
        diagnosis = f'Pocos PDFs publicados ({pdfs} PDFs de {total_resources} recursos)'

    return {
        'id': cid,
        'name': name,
        'period': period,
        'folder': str(folder.relative_to(root)) if folder else None,
        'status': 'procesado',
        'pdfs': pdfs,
        'pages': pages,
        'assignments': assignments,
        'total_resources': total_resources,
        'counts': counts,
        'pocos_pdfs': pocos,
        'diagnosis': diagnosis
    }


def main():
    parser = argparse.ArgumentParser(description='Detecta cursos con pocos PDFs y diagnostica la causa.')
    parser.add_argument('--umbral', type=int, default=5, help='Maximo de PDFs para considerar que un curso tiene pocos (default: 5)')
    parser.add_argument('--all', action='store_true', help='Analizar todos los cursos del inventario de Canvas, no solo los seleccionados')
    parser.add_argument('--course', type=int, action='append', help='Filtrar por ID de curso puntual')
    parser.add_argument('--config', help='Ruta a biblioteca.config.json')
    parser.add_argument('--json', action='store_true', help='Imprimir reporte en formato JSON')
    args = parser.parse_args()

    cfg = load_settings(args.config)
    root = Path(cfg['root']).resolve()

    cursos_json = root / 'cursos.json'
    courses_map = {}
    if cursos_json.exists():
        try:
            for c in json.loads(cursos_json.read_text('utf-8')):
                courses_map[c['id']] = c
        except Exception:
            pass

    if args.course:
        target_ids = args.course
    elif args.all:
        target_ids = list(courses_map.keys())
    else:
        courses_file = Path(cfg['courses_file'])
        if courses_file.exists():
            target_ids = json.loads(courses_file.read_text('utf-8-sig')).get('course_ids', [])
        else:
            target_ids = list(courses_map.keys())

    # Pre-indexar carpetas existentes por ID de curso
    folder_by_id = {}
    for p in root.glob('*/*'):
        if p.is_dir():
            m = re.search(r'\[(\d+)\]$', p.name)
            if m:
                folder_by_id[int(m.group(1))] = p

    results = []
    for cid in target_ids:
        info = courses_map.get(cid, {'id': cid, 'name': f'Curso {cid}'})
        results.append(analyze_course(cid, info, root, args.umbral, folder=folder_by_id.get(cid)))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    # Imprimir en consola de forma estructurada
    alerta_pocos = [r for r in results if r['status'] == 'procesado' and r['pocos_pdfs']]
    saludables = [r for r in results if r['status'] == 'procesado' and not r['pocos_pdfs']]
    no_iniciados = [r for r in results if r['status'] == 'no_iniciado']

    total_pdfs = sum(r['pdfs'] for r in results)
    total_paginas = sum(r['pages'] for r in results)
    total_tareas = sum(r['assignments'] for r in results)
    total_sin_acceso = sum(r['counts'].get('sin_acceso_sharepoint', 0) for r in results)
    total_404 = sum(r['counts'].get('no_encontrado_sharepoint', 0) for r in results)
    total_pendientes = sum(r['counts'].get('pendiente', 0) for r in results)

    print('=' * 105)
    print(f" MONITOREO DE DESCARGAS - CURSOS CON POCOS PDFs (Umbral: <= {args.umbral} PDFs)")
    print('=' * 105)

    if alerta_pocos:
        print(f"\n[!] ATENCION: {len(alerta_pocos)} CURSOS PROCESADOS TIENEN {args.umbral} O MENOS PDFs:\n")
        print(f"{'ID':<8} {'PERIODO':<9} {'PDFs':<6} {'PAG/TAR':<9} {'CURSO':<40} {'DIAGNOSTICO / CAUSA'}")
        print('-' * 105)
        for r in sorted(alerta_pocos, key=lambda x: x['pdfs']):
            cname = (r['name'][:37] + '...') if len(r['name']) > 40 else r['name']
            pag_tar = f"{r['pages']}/{r['assignments']}"
            print(f"{r['id']:<8} {r['period']:<9} {r['pdfs']:<6} {pag_tar:<9} {cname:<40} {r['diagnosis']}")
    else:
        print(f"\n[OK] Ningun curso procesado tiene {args.umbral} o menos PDFs.")

    if saludables:
        print(f"\n[+] CURSOS CON DESCARGA NORMAL (> {args.umbral} PDFs):\n")
        print(f"{'ID':<8} {'PERIODO':<9} {'PDFs':<6} {'PAG/TAR':<9} {'CURSO':<40} {'DIAGNOSTICO'}")
        print('-' * 105)
        for r in sorted(saludables, key=lambda x: x['pdfs'], reverse=True):
            cname = (r['name'][:37] + '...') if len(r['name']) > 40 else r['name']
            pag_tar = f"{r['pages']}/{r['assignments']}"
            print(f"{r['id']:<8} {r['period']:<9} {r['pdfs']:<6} {pag_tar:<9} {cname:<40} {r['diagnosis']}")

    if no_iniciados:
        print(f"\n[-] CURSOS PENDIENTES DE INICIAR ({len(no_iniciados)} cursos aun sin carpeta):\n")
        for r in no_iniciados[:8]:
            print(f"    - [{r['id']}] ({r['period']}) {r['name']}")
        if len(no_iniciados) > 8:
            print(f"    ... y {len(no_iniciados) - 8} cursos mas pendientes.")

    print('\n' + '=' * 105)
    print(" RESUMEN GLOBAL DE LA BIBLIOTECA:")
    print(f"  * Cursos analizados:         {len(results)}")
    print(f"  * Cursos saludables:         {len(saludables)}")
    print(f"  * Cursos con pocos PDFs:     {len(alerta_pocos)}")
    print(f"  * Cursos sin iniciar:        {len(no_iniciados)}")
    print(f"  * Total PDFs en disco:       {total_pdfs} archivos")
    print(f"  * Total Paginas / Tareas:    {total_paginas} paginas, {total_tareas} tareas guardadas")
    if total_sin_acceso or total_404 or total_pendientes:
        print(f"  * Conflictos detectados:     {total_sin_acceso} sin acceso a SharePoint, {total_404} enlaces 404, {total_pendientes} pendientes")
    print('=' * 105)


if __name__ == '__main__':
    main()

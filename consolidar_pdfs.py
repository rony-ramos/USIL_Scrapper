"""Consolida todos los PDFs de cada curso en una carpeta centralizada biblioteca/pdf/{curso}."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from settings import load_settings


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def find_course_directories(root):
    """Busca directorios de cursos bajo root/{periodo}/{curso}, ignorando root/pdf."""
    courses = []
    if not root.exists():
        return courses
    for period_dir in sorted(root.iterdir()):
        if not period_dir.is_dir() or period_dir.name.lower() == 'pdf':
            continue
        for course_dir in sorted(period_dir.iterdir()):
            if not course_dir.is_dir():
                continue
            courses.append(course_dir)
    return courses


def collect_pdfs(course_dir, include_versions=False):
    """Recopila todos los archivos .pdf de un curso, omitiendo .part y versiones si no se piden."""
    pdfs = []
    for p in course_dir.rglob('*'):
        if not p.is_file():
            continue
        name_lower = p.name.lower()
        if not name_lower.endswith('.pdf') or name_lower.endswith('.part'):
            continue
        if not include_versions and 'versiones' in p.parts:
            continue
        pdfs.append(p)
    return sorted(pdfs)


def format_pdf_name(original_name):
    """Mueve el prefijo {codigo}- al final como {nombre} [{codigo}].pdf para ordenar alfabéticamente."""
    m = re.match(r'^(\d+)\s*[-_]\s*(.+?)(\.pdf)$', original_name, re.I)
    if not m:
        return original_name
    code, body, ext = m.group(1), m.group(2).strip(), m.group(3)
    if not body.endswith(f"[{code}]"):
        return f"{body} [{code}]{ext}"
    return f"{body}{ext}"


def consolidate_course(course_dir, dest_course_dir, dry_run=False, force=False):
    """Copia los PDFs de un curso a dest_course_dir con el código al final y resolviendo colisiones."""
    pdfs = collect_pdfs(course_dir)
    if not pdfs:
        return {'total': 0, 'copied': 0, 'skipped': 0, 'renamed': 0, 'migrated': 0}

    if not dry_run:
        dest_course_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    renamed = 0
    migrated = 0

    for src_pdf in pdfs:
        new_name = format_pdf_name(src_pdf.name)
        dest_file = dest_course_dir / new_name
        old_dest_file = dest_course_dir / src_pdf.name

        # Si ya existía con el formato anterior {codigo}-{nombre}.pdf en el destino:
        if old_dest_file.exists() and old_dest_file != dest_file and not dest_file.exists():
            if sha256(old_dest_file) == sha256(src_pdf):
                if not dry_run:
                    old_dest_file.rename(dest_file)
                migrated += 1
                continue
            else:
                if not dry_run:
                    old_dest_file.unlink()
        elif old_dest_file.exists() and old_dest_file != dest_file and dest_file.exists():
            if not dry_run:
                old_dest_file.unlink()

        # Si ya existe un archivo con ese nombre en el destino
        if dest_file.exists() and not force:
            # Si el contenido es idéntico, lo omitimos (idempotencia)
            if sha256(src_pdf) == sha256(dest_file):
                skipped += 1
                continue
            # Si el contenido difiere, desambiguamos con el módulo/semana de origen
            rel_parts = src_pdf.relative_to(course_dir).parts
            prefix = '_'.join(re.sub(r'[<>:"/\\|?*]', '_', part).strip(' .') for part in rel_parts[:-1])
            disambiguated_name = f"{prefix}_{dest_file.name}" if prefix else f"dup_{dest_file.name}"
            dest_file = dest_course_dir / disambiguated_name

            if dest_file.exists():
                if sha256(src_pdf) == sha256(dest_file):
                    skipped += 1
                    continue
                # Como último recurso, agregamos hash parcial
                digest = sha256(src_pdf)[:8]
                dest_file = dest_course_dir / f"{dest_file.stem}_{digest}{dest_file.suffix}"

            renamed += 1

        if not dry_run:
            shutil.copy2(src_pdf, dest_file)
        copied += 1

    return {'total': len(pdfs), 'copied': copied, 'skipped': skipped, 'renamed': renamed, 'migrated': migrated}


def run_consolidation(root, dest_root=None, course_filter=None, dry_run=False, force=False):
    dest_root = (dest_root or (root / 'pdf')).resolve()
    courses = find_course_directories(root)

    if course_filter:
        filter_str = str(course_filter).strip()
        courses = [c for c in courses if filter_str in c.name or (
            (m := re.search(r'\[(\d+)\]$', c.name)) and m.group(1) == filter_str
        )]

    results = {}
    for course_dir in courses:
        dest_dir = dest_root / course_dir.name
        stats = consolidate_course(course_dir, dest_dir, dry_run=dry_run, force=force)
        if stats['total'] > 0:
            results[course_dir.name] = stats

    return results, dest_root


def main():
    parser = argparse.ArgumentParser(description='Consolida los PDFs de cada curso en biblioteca/pdf/{curso}')
    parser.add_argument('--course', help='Filtrar por ID o nombre de curso puntual')
    parser.add_argument('--dest', help='Ruta de destino alternativa (default: biblioteca/pdf)')
    parser.add_argument('--dry-run', action='store_true', help='Simula la copia sin escribir archivos en disco')
    parser.add_argument('--force', action='store_true', help='Sobrescribe archivos incluso si ya existen con igual hash')
    parser.add_argument('--config', help='Ruta a biblioteca.config.json')
    parser.add_argument('--json', action='store_true', help='Imprime el resultado en formato JSON')
    args = parser.parse_args()

    cfg = load_settings(args.config)
    root = Path(cfg['root']).resolve()
    dest_root = Path(args.dest).resolve() if args.dest else (root / 'pdf')

    results, final_dest = run_consolidation(
        root=root,
        dest_root=dest_root,
        course_filter=args.course,
        dry_run=args.dry_run,
        force=args.force
    )

    if args.json:
        print(json.dumps({
            'destino': str(final_dest),
            'cursos': results
        }, ensure_ascii=False, indent=2))
        return

    mode_label = ' [MODO SIMULACION (DRY-RUN)]' if args.dry_run else ''
    print('=' * 95)
    print(f" CONSOLIDACION DE PDFs POR CURSO{mode_label}")
    print(f" Destino base: {final_dest}")
    print('=' * 95)

    if not results:
        print("\nNo se encontraron PDFs para consolidar.")
        return

    total_pdfs = 0
    total_copied = 0
    total_migrated = 0
    total_skipped = 0
    total_renamed = 0

    print(f"\n{'CURSO':<50} {'TOTAL':<7} {'COPIADOS':<9} {'MIGRADOS':<9} {'OMITIDOS':<9} {'RENOMBRADOS':<10}")
    print('-' * 95)

    for course_name, stats in results.items():
        cname = (course_name[:47] + '...') if len(course_name) > 50 else course_name
        mig = stats.get('migrated', 0)
        print(f"{cname:<50} {stats['total']:<7} {stats['copied']:<9} {mig:<9} {stats['skipped']:<9} {stats['renamed']:<10}")
        total_pdfs += stats['total']
        total_copied += stats['copied']
        total_migrated += mig
        total_skipped += stats['skipped']
        total_renamed += stats['renamed']

    print('=' * 95)
    print(" RESUMEN:")
    print(f"  * Cursos con PDFs:        {len(results)}")
    print(f"  * Total PDFs detectados:  {total_pdfs}")
    print(f"  * Copiados nuevos:        {total_copied}")
    if total_migrated:
        print(f"  * Migrados ({'código'} al final): {total_migrated}")
    print(f"  * Omitidos (ya al día):   {total_skipped}")
    if total_renamed:
        print(f"  * Renombrados (colisión): {total_renamed}")
    print(f"  * Carpeta de salida:      {final_dest}")
    print('=' * 95)


if __name__ == '__main__':
    main()

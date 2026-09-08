import io
import shutil
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfWriter
from consolidar_pdfs import find_course_directories, collect_pdfs, consolidate_course, run_consolidation, format_pdf_name


def create_pdf(path, pages=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    with path.open('wb') as f:
        writer.write(f)


class ConsolidarPdfsTests(unittest.TestCase):
    def test_find_course_directories_ignores_pdf_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '2026-02' / 'Curso A [101]').mkdir(parents=True)
            (root / '2026-02' / 'Curso B [102]').mkdir(parents=True)
            (root / 'pdf' / 'Curso A [101]').mkdir(parents=True)
            courses = find_course_directories(root)
            names = [c.name for c in courses]
            self.assertEqual(len(courses), 2)
            self.assertIn('Curso A [101]', names)
            self.assertIn('Curso B [102]', names)

    def test_collect_pdfs_excludes_part_and_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            course_dir = Path(tmp) / 'Curso A [101]'
            create_pdf(course_dir / 'U1' / 'S01' / 'doc1.pdf')
            create_pdf(course_dir / 'U1' / 'S01' / 'doc2.pdf.part')
            create_pdf(course_dir / 'versiones' / 'old_doc.pdf')
            
            pdfs = collect_pdfs(course_dir, include_versions=False)
            self.assertEqual(len(pdfs), 1)
            self.assertEqual(pdfs[0].name, 'doc1.pdf')

    def test_consolidate_course_copies_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course_dir = root / '2026-02' / 'Curso A [101]'
            dest_dir = root / 'pdf' / 'Curso A [101]'
            create_pdf(course_dir / 'M1' / 'W1' / 'guia1.pdf', pages=1)
            create_pdf(course_dir / 'M2' / 'W2' / 'guia2.pdf', pages=2)

            # Primera consolidacion
            stats1 = consolidate_course(course_dir, dest_dir)
            self.assertEqual(stats1['total'], 2)
            self.assertEqual(stats1['copied'], 2)
            self.assertEqual(stats1['skipped'], 0)
            self.assertTrue((dest_dir / 'guia1.pdf').is_file())
            self.assertTrue((dest_dir / 'guia2.pdf').is_file())

            # Segunda consolidacion (debe omitir por sha256 identico)
            stats2 = consolidate_course(course_dir, dest_dir)
            self.assertEqual(stats2['total'], 2)
            self.assertEqual(stats2['copied'], 0)
            self.assertEqual(stats2['skipped'], 2)

    def test_collision_with_different_content_disambiguates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course_dir = root / '2026-02' / 'Curso B [102]'
            dest_dir = root / 'pdf' / 'Curso B [102]'
            # Dos archivos con el mismo nombre pero diferente contenido (diferente numero de paginas)
            create_pdf(course_dir / 'Modulo 1' / 'S01' / 'silabo.pdf', pages=1)
            create_pdf(course_dir / 'Modulo 2' / 'S02' / 'silabo.pdf', pages=3)

            stats = consolidate_course(course_dir, dest_dir)
            self.assertEqual(stats['total'], 2)
            self.assertEqual(stats['copied'], 2)
            self.assertEqual(stats['renamed'], 1)
            
            dest_files = list(dest_dir.glob('*.pdf'))
            self.assertEqual(len(dest_files), 2)
            names = [f.name for f in dest_files]
            self.assertIn('silabo.pdf', names)
            self.assertTrue(any('Modulo 2_S02_silabo.pdf' in n or 'silabo_' in n for n in names))

    def test_format_pdf_name_moves_code_to_brackets_at_end(self):
        self.assertEqual(format_pdf_name('7708286-S01 Mat. Clase.pdf'), 'S01 Mat. Clase [7708286].pdf')
        self.assertEqual(format_pdf_name('12345_documento.pdf'), 'documento [12345].pdf')
        self.assertEqual(format_pdf_name('999 - Tarea Final.pdf'), 'Tarea Final [999].pdf')
        self.assertEqual(format_pdf_name('silabo.pdf'), 'silabo.pdf')
        self.assertEqual(format_pdf_name('archivo [123].pdf'), 'archivo [123].pdf')

    def test_consolidate_course_migrates_existing_prefixed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course_dir = root / '2026-02' / 'Curso C [103]'
            dest_dir = root / 'pdf' / 'Curso C [103]'
            create_pdf(course_dir / 'S01' / '7708286-S01 Mat. Clase.pdf')
            create_pdf(dest_dir / '7708286-S01 Mat. Clase.pdf')

            stats = consolidate_course(course_dir, dest_dir)
            self.assertEqual(stats['total'], 1)
            self.assertEqual(stats['migrated'], 1)
            self.assertFalse((dest_dir / '7708286-S01 Mat. Clase.pdf').exists())
            self.assertTrue((dest_dir / 'S01 Mat. Clase [7708286].pdf').exists())


if __name__ == '__main__':
    unittest.main()

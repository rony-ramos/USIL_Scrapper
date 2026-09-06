import tempfile
import unittest
from pathlib import Path
from pypdf import PdfWriter
from piloto import component, destination, validate


class PilotTests(unittest.TestCase):
    def test_windows_names(self):
        for value in ('..', 'CON', 'NUL.pdf', 'LPT1'):
            with self.assertRaises(ValueError):
                component(value)
        self.assertEqual(component('Tema: A/B?'), 'Tema_ A_B_')

    def test_paths_stay_inside_root(self):
        with tempfile.TemporaryDirectory() as folder:
            resource = dict(periodo='2026-02', curso='../curso', unidad='U1',
                            semana='S1', archivo='a.pdf')
            root, path = destination({'root': folder}, resource)
            self.assertTrue(path.is_relative_to(root))

    def test_reject_html_and_wrong_page_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.pdf'
            path.write_bytes(b'<html>Login</html>')
            with self.assertRaises(ValueError):
                validate(path, 39)
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with path.open('wb') as f:
                writer.write(f)
            with self.assertRaises(ValueError):
                validate(path, 39)
            self.assertEqual(validate(path, 1)['pages'], 1)


if __name__ == '__main__':
    unittest.main()

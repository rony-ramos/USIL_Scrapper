import json
from pathlib import Path
import tempfile
import unittest
from settings import load_settings
from configurar import initialize


class SettingsTests(unittest.TestCase):
    def test_relative_paths_use_config_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'canvas_url': 'https://campus.example.com', 'root': 'data'}))
            cfg = load_settings(path)
            self.assertEqual(Path(cfg['root']), Path(folder).resolve() / 'data')
            self.assertEqual(cfg['sharepoint_hosts'], [])

    def test_missing_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            with self.assertRaisesRegex(ValueError, 'Copia'):
                load_settings(path)
            for url in ['http://campus.example.com', 'https://campus.example.com/path', '']:
                path.write_text(json.dumps({'canvas_url': url}))
                with self.assertRaises(ValueError):
                    load_settings(path)

    def test_setup_preserves_local_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('biblioteca.config', 'cursos_seleccionados', 'piloto'):
                (root / f'{name}.example.json').write_text('{}')
            (root / 'biblioteca.config.json').write_text('{"personal":true}')
            initialize(root)
            self.assertEqual((root / 'biblioteca.config.json').read_text(), '{"personal":true}')
            self.assertTrue((root / 'cursos_seleccionados.json').exists())


if __name__ == '__main__':
    unittest.main()

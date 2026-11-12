import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from pypdf import PdfWriter
from canvas_sync import CanvasAPI, AccessError, active_courses, stream_file, resolve_sharepoint, SharePointUnavailable, SharePointNotFound, retry_files, course_folder, write_json


def response(body, links=None, status=200):
    r = Mock()
    r.status_code = status
    r.text = body
    r.links = links or {}
    return r


class CanvasTests(unittest.TestCase):
    def test_active_courses_limit_current_period(self):
        api = Mock()
        api.all.return_value = [
            {'id': 1, 'name': 'Software - 2021-01'},
            {'id': 2, 'name': 'Proyectos - 2026-02'},
            {'id': 3, 'name': 'Calidad - 2026-02'},
        ]
        self.assertEqual([c['id'] for c in active_courses(api, '2026-02')], [2, 3])
        api.all.assert_called_once_with(
            '/api/v1/courses?per_page=100&enrollment_state=active&state%5B%5D=available')

    def test_retry_canvas_file_uses_host_returned_by_authenticated_api(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            course = {'id': 1, 'name': 'Software - 2026-02'}
            index = course_folder(root, course) / 'recursos.json'
            write_json(index, [{'id': 5, 'content_id': 9, 'type': 'File', 'status': 'pendiente',
                                'module': 'U1', 'week': 'S1'}])
            write_json(root / 'ultima-sincronizacion.json', [{'course': 1}])
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            buffer = io.BytesIO()
            writer.write(buffer)
            response = Mock(status_code=200, is_redirect=False)
            response.headers = {'Content-Type': 'application/pdf'}
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.iter_content.return_value = [buffer.getvalue()]
            api = Mock(base_url='https://campus.example.com')
            api.all.return_value = [course]
            api.get.return_value = ({'filename': 'test.pdf', 'url': 'https://files.example.net/document'}, None)
            api.session.get.return_value = response
            retry_files(api, root, {1}, {'sharepoint_hosts': [], 'download_hosts': []})
            record = json.loads(index.read_text('utf-8'))[0]
            self.assertEqual(record['status'], 'descargado')
            self.assertTrue((root / record['path']).is_file())

    def test_sharepoint_error_stops_without_waiting_for_download(self):
        driver = Mock()
        driver.execute_script.return_value = {'error': True, 'message': 'Lo sentimos, no puede acceder a este documento.'}
        with self.assertRaisesRegex(SharePointUnavailable, 'no puede acceder'):
            resolve_sharepoint(driver, timeout=0.1)
        self.assertEqual(driver.execute_script.call_count, 1)

    def test_sharepoint_404_not_found_stops_immediately(self):
        driver = Mock()
        driver.execute_script.return_value = {'error': True, 'type': 'not_found', 'message': '404 NOT FOUND'}
        with self.assertRaises(SharePointNotFound):
            resolve_sharepoint(driver, timeout=0.1)
        self.assertEqual(driver.execute_script.call_count, 1)

    def test_sharepoint_modern_access_denied_stops_without_waiting(self):
        driver = Mock()
        driver.execute_script.return_value = {
            'error': True,
            'type': 'unavailable',
            'message': 'Necesita acceso: Usted (usuario@usil.pe) no tiene acceso a este objeto elemento.'
        }
        with self.assertRaisesRegex(SharePointUnavailable, 'Necesita acceso'):
            resolve_sharepoint(driver, timeout=0.1)
        self.assertEqual(driver.execute_script.call_count, 1)

    def test_sharepoint_download_endpoint(self):
        driver = Mock()
        driver.execute_script.return_value = {'url': 'https://files.example.com/_layouts/15/download.aspx?id=1'}
        self.assertEqual(resolve_sharepoint(driver), driver.execute_script.return_value['url'])

    def test_pagination_follows_link(self):
        session = Mock()
        next_url = 'https://campus.example.com/api/v1/courses?page=2'
        session.get.side_effect = [response('[{"id":1}]', {'next': {'url': next_url}}),
                                   response('[{"id":2}]')]
        self.assertEqual(CanvasAPI(session, 'https://campus.example.com').all('/api/v1/courses'), [{'id': 1}, {'id': 2}])
        self.assertEqual(session.get.call_args.args[0], next_url)

    def test_untrusted_pagination_never_requested(self):
        session = Mock()
        session.get.return_value = response('[]', {'next': {'url': 'https://example.com/steal'}})
        with self.assertRaises(AccessError):
            CanvasAPI(session, 'https://campus.example.com').all('/api/v1/courses')
        self.assertEqual(session.get.call_count, 1)

    def test_login_html_rejected(self):
        session = Mock()
        session.get.return_value = response('<html>Login</html>')
        with self.assertRaises(AccessError):
            CanvasAPI(session, 'https://campus.example.com').all('/api/v1/courses')

    def test_pdf_stream_idempotency_and_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'course' / 'week' / 'test.pdf'
            for pages, expected in [(1, 'descargado'), (1, 'sin_cambios'), (2, 'descargado')]:
                writer = PdfWriter()
                for _ in range(pages):
                    writer.add_blank_page(width=100, height=100)
                buffer = io.BytesIO()
                writer.write(buffer)
                r = Mock(status_code=200, is_redirect=False)
                r.headers = {'Content-Type': 'application/pdf'}
                r.__enter__ = Mock(return_value=r)
                r.__exit__ = Mock(return_value=False)
                r.iter_content.return_value = [buffer.getvalue()]
                session = Mock()
                session.get.return_value = r
                status, _ = stream_file(session, 'https://campus.example.com/file', target, {'campus.example.com'})
                self.assertEqual(status, expected)
                self.assertFalse(target.with_suffix('.pdf.part').exists())
            self.assertEqual(len(list((target.parent / 'versiones').glob('*.pdf'))), 1)


def pdf_bytes(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class IncrementalSyncTests(unittest.TestCase):
    """La sincronizacion reusa copias verificadas salvo que se fuerce."""
    COURSE = {'id': 1, 'name': 'Software - 2026-02'}
    FILE_ITEM = {'id': 5, 'title': 'Lectura', 'type': 'File', 'content_id': 9}
    SP_ITEM = {'id': 6, 'title': 'Diapositivas', 'type': 'ExternalUrl',
               'external_url': 'https://sp.example.com/:b:/doc'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = course_folder(self.root, self.COURSE)
        self.content = pdf_bytes()
        self.local = self.folder / 'U1' / 'General' / '5-lectura.pdf'
        self.local.parent.mkdir(parents=True)
        self.local.write_bytes(self.content)
        self.config = {'sharepoint_hosts': ['sp.example.com'], 'download_hosts': []}

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, **changes):
        from piloto import sha256
        base = {'id': 5, 'type': 'File', 'content_id': 9, 'status': 'descargado',
                'path': str(self.local.relative_to(self.root)), 'sha256': sha256(self.local)}
        base.update(changes)
        return base

    def run_sync(self, prior, items, detail=None, body=None, **kwargs):
        from canvas_sync import sync
        write_json(self.folder / 'recursos.json', prior)
        api = Mock(base_url='https://campus.example.com')
        api.all.side_effect = lambda url: [{'id': 3, 'name': 'U1'}] if url.endswith('/modules?per_page=100') else items
        api.get.return_value = (detail or {'filename': 'lectura.pdf', 'size': len(self.content),
                                           'updated_at': '2026-09-01T00:00:00Z', 'modified_at': '2026-09-01T00:00:00Z',
                                           'url': 'https://files.example.net/signed?verifier=secret'}, None)
        r = Mock(status_code=200, is_redirect=False)
        r.headers = {'Content-Type': 'application/pdf'}
        r.__enter__ = Mock(return_value=r)
        r.__exit__ = Mock(return_value=False)
        r.iter_content.return_value = [body or self.content]
        api.session.get.return_value = r
        driver = Mock()
        sync(driver, api, self.root, {1}, kwargs.pop('download', True), self.config, courses=[self.COURSE], **kwargs)
        return json.loads((self.folder / 'recursos.json').read_text('utf-8')), api, driver

    def remote(self, **changes):
        value = {'filename': 'lectura.pdf', 'size': len(self.content),
                 'updated_at': '2026-09-01T00:00:00Z', 'modified_at': '2026-09-01T00:00:00Z'}
        value.update(changes)
        return value

    def test_unchanged_canvas_metadata_skips_download(self):
        records, api, _ = self.run_sync([self.record(remote=self.remote())], [self.FILE_ITEM])
        self.assertEqual(records[0]['status'], 'existente_verificado')
        api.session.get.assert_not_called()
        self.assertNotIn('verifier', json.dumps(records))

    def test_changed_canvas_metadata_downloads_new_version(self):
        old = self.record(remote=self.remote(updated_at='2026-08-01T00:00:00Z'))
        records, api, _ = self.run_sync([old], [self.FILE_ITEM], body=pdf_bytes(2))
        self.assertEqual(records[0]['status'], 'descargado')
        self.assertEqual(records[0]['remote']['updated_at'], '2026-09-01T00:00:00Z')
        self.assertEqual(len(list((self.local.parent / 'versiones').glob('*.pdf'))), 1)
        self.assertNotIn('verifier', json.dumps(records))

    def test_legacy_record_adopted_by_exact_size_without_download(self):
        records, api, _ = self.run_sync([self.record()], [self.FILE_ITEM])
        self.assertEqual(records[0]['status'], 'existente_verificado')
        self.assertEqual(records[0]['remote'], self.remote())
        api.session.get.assert_not_called()

    def test_legacy_record_with_different_size_downloads(self):
        detail = {'filename': 'lectura.pdf', 'size': len(self.content) + 1, 'updated_at': 'x', 'modified_at': 'x',
                  'url': 'https://files.example.net/signed'}
        records, api, _ = self.run_sync([self.record()], [self.FILE_ITEM], detail=detail)
        api.session.get.assert_called()
        self.assertEqual(records[0]['status'], 'sin_cambios')

    def test_corrupted_local_copy_is_downloaded_again(self):
        old = self.record(remote=self.remote())
        self.local.write_bytes(b'alterado')
        records, api, _ = self.run_sync([old], [self.FILE_ITEM])
        api.session.get.assert_called()
        self.assertEqual(records[0]['status'], 'descargado')

    def test_force_downloads_even_when_unchanged(self):
        records, api, _ = self.run_sync([self.record(remote=self.remote())], [self.FILE_ITEM], force=True)
        api.session.get.assert_called()
        self.assertEqual(records[0]['status'], 'sin_cambios')

    def test_inventory_without_download_keeps_local_reference(self):
        records, api, _ = self.run_sync([self.record(remote=self.remote())], [self.FILE_ITEM], download=False)
        self.assertEqual(records[0]['status'], 'existente_verificado')
        self.assertTrue(records[0]['path'])
        api.get.assert_not_called()

    def test_sharepoint_unavailable_skipped_unless_rechecked(self):
        old = {'id': 6, 'type': 'ExternalUrl', 'external_url': self.SP_ITEM['external_url'],
               'status': 'sin_acceso_sharepoint', 'message': 'Necesita acceso'}
        records, _, driver = self.run_sync([old], [self.SP_ITEM])
        self.assertEqual(records[0]['status'], 'sin_acceso_sharepoint')
        driver.get.assert_not_called()
        driver.execute_script.return_value = {'error': True, 'type': 'unavailable', 'message': 'Necesita acceso'}
        records, _, driver = self.run_sync([old], [self.SP_ITEM], recheck_unavailable=True)
        driver.get.assert_called_once()

    def test_verified_sharepoint_copy_is_not_opened_again(self):
        old = self.record(id=6, type='ExternalUrl', content_id=None, external_url=self.SP_ITEM['external_url'])
        old.pop('content_id')
        records, _, driver = self.run_sync([old], [self.SP_ITEM])
        self.assertEqual(records[0]['status'], 'existente_verificado')
        driver.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()

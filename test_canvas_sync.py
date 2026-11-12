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


class HostPolicyTests(unittest.TestCase):
    def test_suffix_allows_only_subdomains_of_that_exact_domain(self):
        from canvas_sync import HostPolicy
        policy = HostPolicy({'campus.example.com'}, ['cluster9.files.example.net']) | {'cdn.example.org'}
        for host in ('campus.example.com', 'cdn.example.org', 'a1-22.cluster9.files.example.net'):
            self.assertIn(host, policy)
        for host in ('cluster9.files.example.net', 'evilcluster9.files.example.net', 'cluster9.files.example.net.evil.com',
                     'a1.cluster8.files.example.net', '', None):
            self.assertNotIn(host, policy)

    def test_settings_reject_broad_suffixes(self):
        from settings import load_settings
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'c.json'
            for bad in (['example.net'], ['*.example.net'], ['.files.example.net'], ['files.example.net/x']):
                path.write_text(json.dumps({'canvas_url': 'https://campus.example.com', 'download_host_suffixes': bad}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_settings(path)
            path.write_text(json.dumps({'canvas_url': 'https://campus.example.com',
                                        'download_host_suffixes': ['cluster9.files.example.net']}), encoding='utf-8')
            self.assertEqual(load_settings(path)['download_host_suffixes'], ['cluster9.files.example.net'])


class DeduplicationTests(unittest.TestCase):
    def test_identical_files_become_hardlinks_and_record_duplicate_of(self):
        import os
        from canvas_sync import deduplicate
        from piloto import sha256
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a = course_folder(root, {'id': 1, 'name': 'A - 2026-02'})
            b = course_folder(root, {'id': 2, 'name': 'B - 2026-02'})
            content, other = pdf_bytes(1), pdf_bytes(2)
            files = {a / 'U1' / '5-x.pdf': content, b / 'U2' / '7-x.pdf': content, b / 'U2' / '8-y.pdf': other}
            for path, data in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            rel = lambda p: str(p.relative_to(root))
            write_json(a / 'recursos.json', [{'id': 5, 'path': rel(a / 'U1' / '5-x.pdf'), 'sha256': sha256(a / 'U1' / '5-x.pdf')}])
            write_json(b / 'recursos.json', [{'id': 7, 'path': rel(b / 'U2' / '7-x.pdf'), 'sha256': sha256(b / 'U2' / '7-x.pdf')},
                                             {'id': 8, 'path': rel(b / 'U2' / '8-y.pdf'), 'sha256': sha256(b / 'U2' / '8-y.pdf')}])
            report = deduplicate(root, [a, b])
            self.assertEqual((report['duplicates'], report['linked']), (1, 1))
            self.assertTrue(os.path.samefile(a / 'U1' / '5-x.pdf', b / 'U2' / '7-x.pdf'))
            self.assertEqual((b / 'U2' / '7-x.pdf').read_bytes(), content)
            records = {r['id']: r for r in json.loads((b / 'recursos.json').read_text('utf-8'))}
            self.assertEqual(records[7]['duplicate_of'], Path(rel(a / 'U1' / '5-x.pdf')).as_posix())
            self.assertNotIn('duplicate_of', records[8])
            self.assertNotIn('duplicate_of', json.loads((a / 'recursos.json').read_text('utf-8'))[0])
            again = deduplicate(root, [a, b])
            self.assertEqual((again['duplicates'], again['linked']), (1, 0))

    def test_modified_file_is_not_linked(self):
        import os
        from canvas_sync import deduplicate
        from piloto import sha256
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a = course_folder(root, {'id': 1, 'name': 'A - 2026-02'})
            for name in ('1-x.pdf', '2-x.pdf'):
                (a / name).parent.mkdir(parents=True, exist_ok=True)
                (a / name).write_bytes(pdf_bytes(1))
            digest = sha256(a / '1-x.pdf')
            write_json(a / 'recursos.json', [{'id': 1, 'path': str((a / '1-x.pdf').relative_to(root)), 'sha256': digest},
                                             {'id': 2, 'path': str((a / '2-x.pdf').relative_to(root)), 'sha256': digest}])
            (a / '2-x.pdf').write_bytes(b'alterado')
            self.assertEqual(deduplicate(root, [a])['linked'], 0)
            self.assertFalse(os.path.samefile(a / '1-x.pdf', a / '2-x.pdf'))


if __name__ == '__main__':
    unittest.main()

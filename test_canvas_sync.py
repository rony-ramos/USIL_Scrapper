import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from pypdf import PdfWriter
from canvas_sync import CanvasAPI, AccessError, stream_file, resolve_sharepoint, SharePointUnavailable, retry_files, course_folder, write_json


def response(body, links=None, status=200):
    r = Mock()
    r.status_code = status
    r.text = body
    r.links = links or {}
    return r


class CanvasTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()

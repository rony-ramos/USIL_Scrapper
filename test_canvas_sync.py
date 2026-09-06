import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from pypdf import PdfWriter
from canvas_sync import CanvasAPI, AccessError, stream_file


def response(body, links=None, status=200):
    r = Mock()
    r.status_code = status
    r.text = body
    r.links = links or {}
    return r


class CanvasTests(unittest.TestCase):
    def test_pagination_follows_link(self):
        session = Mock()
        next_url = 'https://usil.instructure.com/api/v1/courses?page=2'
        session.get.side_effect = [response('[{"id":1}]', {'next': {'url': next_url}}),
                                   response('[{"id":2}]')]
        self.assertEqual(CanvasAPI(session).all('/api/v1/courses'), [{'id': 1}, {'id': 2}])
        self.assertEqual(session.get.call_args.args[0], next_url)

    def test_untrusted_pagination_never_requested(self):
        session = Mock()
        session.get.return_value = response('[]', {'next': {'url': 'https://example.com/steal'}})
        with self.assertRaises(AccessError):
            CanvasAPI(session).all('/api/v1/courses')
        self.assertEqual(session.get.call_count, 1)

    def test_login_html_rejected(self):
        session = Mock()
        session.get.return_value = response('<html>Login</html>')
        with self.assertRaises(AccessError):
            CanvasAPI(session).all('/api/v1/courses')

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
                status, _ = stream_file(session, 'https://usil.instructure.com/file', target)
                self.assertEqual(status, expected)
                self.assertFalse(target.with_suffix('.pdf.part').exists())
            self.assertEqual(len(list((target.parent / 'versiones').glob('*.pdf'))), 1)


if __name__ == '__main__':
    unittest.main()

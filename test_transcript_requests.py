import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from canvas_sync import write_json
from knowledge_core import load_json, organize, save_recording
from manual_transcripts import import_vtt
from test_knowledge import VTT, context
from transcript_requests import associate_pending, import_curls, parse_curls
from zoom_capture import recording_file_metadata, recording_from_row


URL = 'https://us02web.zoom.us/rec/play/vtt?type=transcript&fid=observed-fid&action=play'
VIEWER = 'https://us02web.zoom.us/rec/play/observed-viewer'
CURL = f"curl --url '{URL}' \\\n  -H 'referer: {VIEWER}' \\\n  -b 'session=example-secret'"


class TranscriptRequestTests(unittest.TestCase):
    def import_one(self, ctx, raw=CURL, body=VTT):
        with patch('transcript_requests.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            response = session.get.return_value.__enter__.return_value
            response.status_code, response.content = 200, body.encode('utf-8')
            report = import_curls(ctx, raw)
            self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
            return report

    def metadata(self, ctx, occurrence='observed-occurrence'):
        record = recording_from_row(99, {'meetingId': occurrence, 'meetingNumber': '123456789',
            'startTime': '2026-10-08 00:03:53', 'timezone': 'America/Los_Angeles'})
        payload = {'meetingId': occurrence, 'meetingNumber': '123456789', 'timezone': 'America/Los_Angeles',
            'recordingFiles': [{'id': 'observed-file', 'fileType': 'MP4',
                'recordingStart': '2026-10-07 17:03:53', 'recordingEnd': '2026-10-07 20:03:53', 'playUrl': VIEWER}]}
        folder, manifest = save_recording(ctx, {**record, 'zoom_detail': recording_file_metadata(payload, record)})
        return folder, manifest

    def test_windows_and_posix_curl_are_parsed_as_data_and_dry_run_has_no_secrets(self):
        windows = f'curl --url ^"{URL}^" ^\n -H ^"referer: {VIEWER}^" ^\n -b ^"session=example-secret^" ]'
        self.assertEqual(parse_curls(windows), parse_curls(CURL))
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder))
            with patch('transcript_requests.requests.Session') as session:
                report = import_curls(ctx, CURL, dry_run=True)
                session.assert_not_called()
            self.assertNotIn('example-secret', json.dumps(report))
            self.assertFalse(ctx['root'].exists())
        with self.assertRaises(ValueError):
            parse_curls(CURL+' -X POST')
        with self.assertRaises(ValueError):
            parse_curls(CURL.replace('us02web.zoom.us', 'zoom.us.attacker.example'))

    def test_originals_and_processed_paths_survive_verified_date_association(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder)); self.import_one(ctx)
            source_path = next(ctx['root'].glob('transcripciones-sin-fecha/*/fuente.json'))
            source = load_json(source_path); original = ctx['root']/source['transcripts'][0]['path']
            self.assertEqual(original.read_bytes(), VTT.encode())
            processed = source_path.parent/'procesado'/'transcripciones'/source['id']/'source-hash'/'model'
            write_json(processed/'estado.json', {'source_path': source['transcripts'][0]['path'],
                'source_sha256': source['transcripts'][0]['sha256'], 'processed_segments': 2,
                'expected_segments': 2, 'status': 'complete', 'model': 'test'})
            (processed/'corregida.md').write_text('Texto revisado', encoding='utf-8')
            (processed/'estudio.md').write_text('Texto academico', encoding='utf-8')
            class_folder, row = self.metadata(ctx)
            write_json(ctx['root']/'grabaciones.json', [{'class_id': row['id'],
                'status': 'metadata_captured', 'transcript_status': 'pending_import'}])
            self.assertEqual(associate_pending(ctx)[0]['status'], 'associated')
            self.assertEqual(load_json(ctx['root']/'grabaciones.json')[0]['transcript_status'], 'imported')
            linked = load_json(class_folder/'clase.json')['transcripts'][0]
            self.assertEqual(linked['path'], source['transcripts'][0]['path'])
            self.assertEqual(linked['processing_manifest_path'], source_path.relative_to(ctx['root']).as_posix())
            self.assertEqual(original.read_bytes(), VTT.encode())
            self.assertEqual(load_json(source_path)['associated_class_id'], row['id'])
            organize(ctx)
            index = (ctx['root']/'indice.md').read_text('utf-8')
            self.assertIn('Clase 2026-10-07T19:03:53-05:00', index)
            self.assertIn((processed/'corregida.md').relative_to(ctx['root']).as_posix(), index)
            self.assertEqual(associate_pending(ctx), [])
            for path in ctx['root'].rglob('*.json'):
                text = path.read_text('utf-8')
                self.assertNotIn('example-secret', text)
                self.assertNotIn(VIEWER, text)

    def test_changed_viewer_does_not_overwrite_existing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder)); self.import_one(ctx)
            original = next(ctx['root'].glob('transcripciones-sin-fecha/*/fuentes/original.vtt'))
            report = self.import_one(ctx, CURL.replace(VIEWER, VIEWER+'-other'), VTT.replace('neuron', 'cambio'))
            self.assertEqual(report[0]['status'], 'pending')
            self.assertEqual(original.read_bytes(), VTT.encode())

    def test_ambiguous_metadata_does_not_assign_a_class(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder)); self.import_one(ctx)
            self.metadata(ctx); self.metadata(ctx, 'second-occurrence')
            report = associate_pending(ctx)
            self.assertEqual(report[0]['status'], 'pending_date')
            self.assertIn('varias', report[0]['reason'])
            for p in ctx['root'].glob('semanas/*/clases/*/clase.json'):
                self.assertEqual(load_json(p)['transcripts'], [])

    def test_manual_association_has_priority_over_automatic_match(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder)); self.import_one(ctx)
            self.metadata(ctx)
            original = Path(folder)/'manual.vtt'
            original.write_bytes(VTT.encode())
            manual = import_vtt(ctx, original, started_at='2026-09-09T19:00:00-05:00')
            result = associate_pending(ctx)[0]
            self.assertEqual(result['status'], 'pending_date')
            self.assertIn('asociacion_existente', result['reason'])
            matching = [load_json(p) for p in ctx['root'].glob('semanas/*/clases/*/clase.json')
                        if load_json(p)['id'] == manual['class_id']]
            self.assertEqual(len(matching[0]['transcripts']), 1)

    def test_redirect_does_not_follow_or_save_html(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder))
            with patch('transcript_requests.requests.Session') as factory:
                session = factory.return_value.__enter__.return_value
                response = session.get.return_value.__enter__.return_value
                response.status_code, response.content = 302, b'<html>login</html>'
                self.assertEqual(import_curls(ctx, CURL)[0]['http'], 302)
                self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
            self.assertFalse(ctx['root'].exists())


if __name__ == '__main__':
    unittest.main()

import base64
import json
from pathlib import Path
import tempfile
import unittest
import time
from unittest.mock import MagicMock, patch

from agent_tasks import (PDF_SCHEMA, TRANSCRIPT_SCHEMA, extract_agy_result, normalize_agy_result,
                         process_transcript, run_agy, validate_page, validate_segments)
from knowledge_core import (archive_text, calendar, inside, load_json, material_catalog, organize,
                            parse_vtt, recording_identity, save_recording, transcript_verified, week_for_date)
from manual_transcripts import import_manifest, import_vtt
from zoom_capture import (ZoomPending, decode_body, public_record, recording_file_metadata,
                          recording_from_row, sync_zoom, transcript_request, viewer_reference)


VTT = 'WEBVTT\r\n\r\n1\r\n00:00:01.000 --> 00:00:02.500\r\n<v Docente>Hola &amp; bienvenidos\r\n\r\n2\r\n00:00:03.000 --> 00:00:04.000\r\nUna red neuronal.\r\n'


def context(root):
    return {'root': root / 'knowledge', 'library': root / 'library',
            'course': {'id': 99, 'name': 'Agentes - 2026-02'},
            'timezone': 'America/Lima',
            'manual': {'pdf_weeks': {}, 'classes': {}, 'calendar': []},
            'records': [
                {'id': 1, 'title': 'Del 17/08/26 al 12/09/26', 'module': 'Unidad 01'},
                *[{'id': 10+i, 'title': f'S{i:02d} material.pdf', 'path': f'p{i}.pdf',
                   'module': '01 Saberes', 'week': f'S{i:02d}'} for i in range(1, 5)]
            ]}


class KnowledgeTests(unittest.TestCase):
    def test_vtt_retains_order_times_and_voice(self):
        cues = parse_vtt(VTT)
        self.assertEqual([c['id'] for c in cues], [1, 2])
        self.assertEqual(cues[0]['start_ms'], 1000)
        self.assertEqual(cues[0]['end_ms'], 2500)
        self.assertEqual(cues[0]['text'], 'Docente: Hola & bienvenidos')
        for invalid in ('<html>login</html>', 'WEBVTT\n\n1\n00:00:02.000 --> 00:00:01.000\nx', 'WEBVTT'):
            with self.assertRaises(ValueError):
                parse_vtt(invalid)

    def test_calendar_infers_only_consistent_published_intervals(self):
        ctx = context(Path('unused'))
        ranges = calendar(ctx)
        self.assertEqual(len(ranges), 4)
        self.assertEqual(week_for_date('2026-09-09', ranges), 'S04')
        self.assertIsNone(week_for_date('2026-09-13', ranges))
        ctx['records'][0]['title'] = 'Del 17/08/26 al 30/09/26'
        self.assertEqual(calendar(ctx), [])

    def test_conflicting_pdf_week_remains_pending_until_manual_assignment(self):
        ctx = context(Path('unused'))
        ctx['records'][1]['title'] = 'S02 material.pdf'
        rows, issues = material_catalog(ctx)
        self.assertIsNone(rows[0]['week'])
        self.assertTrue(any('conflicto' in v for v in issues))
        ctx['manual']['pdf_weeks']['11'] = 'S01'
        self.assertEqual(material_catalog(ctx)[0][0]['week'], 'S01')

    def test_dry_run_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder))
            self.assertEqual(organize(ctx, dry_run=True)['classes'], 0)
            self.assertFalse(ctx['root'].exists())

    def test_same_meeting_and_time_flags_duplicate_without_merging_manual_class(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder))
            common = {'meeting_id': '123456789', 'started_at_utc': '2026-09-09T00:00:00+00:00',
                      'source_timezone': 'America/Los_Angeles', 'topic': 'Clase'}
            first, _ = save_recording(ctx, {**common, 'class_id': recording_identity(
                99, common['meeting_id'], common['started_at_utc'])})
            second, _ = save_recording(ctx, {**common, 'class_id': recording_identity(
                99, common['meeting_id'], common['started_at_utc'], 'verified-occurrence')})
            result = organize(ctx, dry_run=True)
            self.assertEqual(result['classes'], 2)
            self.assertTrue(any('posible duplicado' in issue for issue in result['pending']))
            self.assertTrue((first/'clase.json').is_file())
            self.assertTrue((second/'clase.json').is_file())

    def test_manual_vtt_preserves_bytes_is_incremental_and_versions_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ctx = context(root)
            source = root / 'original.vtt'
            source.write_bytes(VTT.encode('utf-8'))
            options = {'started_at': '2026-09-09T19:01:00-05:00'}
            preview = import_vtt(ctx, source, dry_run=True, **options)
            self.assertFalse(ctx['root'].exists())
            first = import_vtt(ctx, source, **options)
            self.assertEqual(first['class_id'], preview['class_id'])
            self.assertTrue(transcript_verified(ctx, first['class_id']))
            target = next(ctx['root'].glob('semanas/*/clases/*/fuentes/*.vtt'))
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(import_vtt(ctx, source, **options)['status'], 'sin_cambios')
            source.write_bytes(VTT.replace('Una red neuronal.', 'Red neuronal corregida.').encode())
            import_vtt(ctx, source, **options)
            self.assertEqual(len(list(target.parent.glob('versiones/*.vtt'))), 1)
            organize(ctx)
            manifest = load_json(next(ctx['root'].glob('semanas/*/clases/*/clase.json')))
            self.assertEqual(manifest['week'], 'S04')
            self.assertEqual(manifest['materials'], [])
            self.assertEqual(len(manifest['shared_week_materials']), 1)

    def test_manual_vtt_can_attach_to_inventoried_zoom_class(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ctx = context(root)
            record = {'class_id': recording_identity(99, '100', '2026-09-10T00:01:00+00:00', 'uuid'),
                      'meeting_id': '100', 'started_at_utc': '2026-09-10T00:01:00+00:00'}
            save_recording(ctx, record)
            source = root / 'manual.vtt'
            source.write_bytes(VTT.encode())
            imported = import_vtt(ctx, source, class_id=record['class_id'])
            self.assertEqual(imported['class_id'], record['class_id'])
            self.assertEqual(len(list(ctx['root'].glob('semanas/*/clases/*/clase.json'))), 1)
            with self.assertRaises(ValueError):
                import_vtt(ctx, source, started_at='2026-09-09T19:01:00')

    def test_manual_manifest_validates_all_before_importing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ctx = context(root)
            (root / 'valid.vtt').write_bytes(VTT.encode())
            manifest = root / 'batch.json'
            manifest.write_text(json.dumps({'transcripts': [
                {'path': 'valid.vtt', 'started_at': '2026-09-09T19:01:00-05:00'},
                {'path': 'missing.vtt', 'started_at': '2026-09-10T19:01:00-05:00'}]}))
            with self.assertRaises(FileNotFoundError):
                import_manifest(ctx, manifest)
            self.assertFalse(ctx['root'].exists())

    def test_manual_batch_cannot_overwrite_two_parts_using_same_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ctx = context(root)
            (root / 'one.vtt').write_bytes(VTT.encode())
            (root / 'two.vtt').write_bytes(VTT.encode())
            manifest = root / 'batch.json'
            manifest.write_text(json.dumps({'transcripts': [
                {'path': filename, 'started_at': '2026-09-09T19:01:00-05:00'}
                for filename in ('one.vtt', 'two.vtt')]}))
            with self.assertRaisesRegex(ValueError, 'transcript_key'):
                import_manifest(ctx, manifest)
            self.assertFalse(ctx['root'].exists())

    def test_source_paths_cannot_escape_library(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                inside(Path(folder) / 'library', '../secret')


class AgentValidationTests(unittest.TestCase):
    def corrected(self):
        return {'segments': [{'id': i, 'corrected_text': text, 'category': 'academic',
                              'reason': 'Contenido de clase', 'uncertainties': []}
                             for i, text in ((1, 'Hola.'), (2, 'Una red neuronal.'))]}

    def test_agy_output_metadata_does_not_relax_segment_validation(self):
        emitted = {**self.corrected(), 'toolAction': 'Done', 'toolSummary': 'Result'}
        normalized = normalize_agy_result(emitted)
        self.assertEqual(len(validate_segments(normalized, parse_vtt(VTT))), 2)
        normalized['other_field'] = 'unexpected'
        with self.assertRaises(ValueError):
            validate_segments(normalized, parse_vtt(VTT))

    def test_native_structured_output_and_repeated_text_json(self):
        result = self.corrected()
        validator = lambda r: validate_segments(r, parse_vtt(VTT))
        native = {'structured_output': {**result, 'toolAction': 'Done'}, 'response': ''}
        self.assertEqual(extract_agy_result(native, TRANSCRIPT_SCHEMA, validator), result)
        repeated = {'response': json.dumps(result) + '\n' + json.dumps({**result, 'toolSummary': 'Done'})}
        self.assertEqual(extract_agy_result(repeated, TRANSCRIPT_SCHEMA, validator), result)
        with self.assertRaises(ValueError):
            extract_agy_result({'structured_output': {'segments': []}}, TRANSCRIPT_SCHEMA, validator)

    def test_agent_cannot_omit_reorder_erase_or_change_timestamps(self):
        source = parse_vtt(VTT)
        result = validate_segments(self.corrected(), source)
        self.assertEqual(result[0]['start_ms'], source[0]['start_ms'])
        invalid = self.corrected()
        invalid['segments'].reverse()
        with self.assertRaises(ValueError):
            validate_segments(invalid, source)
        invalid = self.corrected()
        invalid['segments'][0]['corrected_text'] = ''
        with self.assertRaises(ValueError):
            validate_segments(invalid, source)
        invalid = self.corrected()
        invalid['segments'][0]['start_ms'] = 999
        with self.assertRaises(ValueError):
            validate_segments(invalid, source)

    def test_pdf_requires_actual_visual_review_and_page_number(self):
        page = {'page': 1, 'visual_review': True, 'status': 'ok', 'title': 'Redes',
                'explanation': 'Diagrama', 'concepts': [], 'visual_elements': [],
                'equations': [], 'uncertainties': []}
        self.assertEqual(validate_page(page, 1), page)
        for changed in ({'page': 2}, {'visual_review': False}, {'visual_elements': ['invalid']}):
            with self.assertRaises(ValueError):
                validate_page({**page, **changed}, 1)

    def test_semantic_failure_is_not_cached_as_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            config = {'command': 'agy', 'model': 'gemini-3.8-flash-high', 'timeout_seconds': 5}
            response = type('Response', (), {'returncode': 0, 'stdout': json.dumps(
                {'status': 'SUCCESS', 'response': '{"segments": []}'})})()
            with patch('agent_tasks.shutil.which', return_value='agy.exe'), patch('agent_tasks.subprocess.run', return_value=response):
                with self.assertRaises(ValueError):
                    run_agy(directory, config, {'segments': parse_vtt(VTT)}, TRANSCRIPT_SCHEMA,
                            'correct-transcript', validator=lambda r: validate_segments(r, parse_vtt(VTT)))
            self.assertEqual(load_json(directory / 'resultado.json')['status'], 'pending')

    def test_transcript_pilot_is_partial_and_preserves_full_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ctx = context(root)
            source = root / 'original.vtt'
            source.write_bytes(VTT.encode())
            import_vtt(ctx, source, started_at='2026-09-09T19:01:00-05:00')
            path = next(ctx['root'].glob('semanas/*/clases/*/clase.json'))
            manifest = load_json(path)
            manifest['manifest_path'] = path.relative_to(ctx['root']).as_posix()
            transcript = manifest['transcripts'][0]
            result = self.corrected()
            result['segments'] = result['segments'][:1]
            with patch('agent_tasks.run_agy', return_value=result):
                report = process_transcript(ctx, manifest, transcript,
                    {'model':'gemini-3.8-flash-high','transcript_chunk_size':1}, block_limit=1)
            self.assertEqual(report['status'], 'partial')
            self.assertEqual(report['expected_segments'], 2)
            self.assertEqual(report['processed_segments'], 1)
            self.assertEqual(report['remaining_segments'], 1)
            self.assertTrue(transcript_verified(ctx, manifest['id']))

    def test_parallel_blocks_are_written_in_original_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ctx = context(root)
            source = root / 'original.vtt'
            source.write_bytes(VTT.encode())
            import_vtt(ctx, source, started_at='2026-09-09T19:01:00-05:00')
            path = next(ctx['root'].glob('semanas/*/clases/*/clase.json'))
            manifest = load_json(path)
            manifest['manifest_path'] = path.relative_to(ctx['root']).as_posix()
            def respond(directory, config, data, *args):
                first = data['segments'][0]
                if first['id'] == 1:
                    time.sleep(0.04)
                return {'segments': [self.corrected()['segments'][first['id']-1]]}
            with patch('agent_tasks.run_agy', side_effect=respond):
                report = process_transcript(ctx, manifest, manifest['transcripts'][0],
                    {'model':'gemini-3.8-flash-high','transcript_chunk_size':1,'workers':2})
            rows = load_json(Path(report['output']) / 'segmentos.json')
            self.assertEqual([r['id'] for r in rows], [1, 2])
            self.assertEqual(report['status'], 'complete')


class ZoomMetadataTests(unittest.TestCase):
    def test_sync_collects_metadata_without_opening_playback_or_downloading_vtt(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder))
            record = recording_from_row(99, {'startTime': '2026-10-08 00:03:53',
                'meetingNumber': '10000000001', 'meetingId': 'observed-occurrence',
                'timezone': 'America/Los_Angeles'})
            detail = recording_file_metadata(self.file_payload(), record)
            driver, session, api = MagicMock(), MagicMock(), MagicMock()
            driver.current_url = 'https://canvas.example.test/courses'
            api.all.return_value = [{'label': 'Zoom', 'html_url': '/courses/99/external_tools/568'}]
            with patch('seleniumbase.Driver', return_value=driver), \
                 patch('zoom_capture.browser_session', return_value=session), \
                 patch('zoom_capture.CanvasAPI', return_value=api), \
                 patch('zoom_capture.active_courses', return_value=[{'id': 99}]), \
                 patch('zoom_capture.zoom_frame'), \
                 patch('zoom_capture.discover', return_value=[record]), \
                 patch('zoom_capture.select_recording', return_value=detail):
                report = sync_zoom({'browser_profile': str(Path(folder)/'profile'),
                                    'canvas_url': 'https://canvas.example.test'}, ctx)
            self.assertEqual(report['metadata_classes'], 1)
            inventory = load_json(ctx['root']/'grabaciones.json')
            self.assertEqual(inventory[0]['status'], 'metadata_captured')
            self.assertEqual(inventory[0]['transcript_status'], 'pending_import')
            self.assertEqual([call.args[0] for call in driver.get.call_args_list],
                             ['https://canvas.example.test/courses', 'https://canvas.example.test/courses/99/external_tools/568'])
            driver.execute_cdp_cmd.assert_not_called()
            driver.switch_to.window.assert_not_called()
            self.assertEqual(list(ctx['root'].rglob('*.vtt')), [])

    def file_payload(self, **changes):
        return {'meetingId': 'observed-occurrence', 'meetingNumber': '10000000001',
                'timezone': 'Hora del pacífico (EE. UU. y Canadá)',
                'recordingFiles': [{'id': 'observed-file', 'fileType': 'MP4',
                    'recordingStart': '2026-10-07 17:03:53', 'recordingEnd': '2026-10-07 20:32:03',
                    'createTime': '2026-10-08 04:50:01',
                    'playUrl': 'https://us02web.zoom.us/rec/play/observed-viewer?auth=private-value'}], **changes}

    def test_file_metadata_uses_recording_date_and_persists_only_hashes(self):
        payload = self.file_payload()
        detail = recording_file_metadata(payload)
        file = detail['files'][0]
        self.assertEqual(file['started_at_utc'], '2026-10-08T00:03:53+00:00')
        self.assertEqual(file['recording_reference'], viewer_reference(payload['recordingFiles'][0]['playUrl']))
        self.assertEqual(len(file['file_id_sha256']), 64)
        encoded = json.dumps(detail)
        for secret in ('private-value', 'observed-viewer', 'observed-occurrence', 'observed-file', 'createTime', 'playUrl'):
            self.assertNotIn(secret, encoded)

    def test_detail_must_match_occurrence_not_just_recurring_meeting_number(self):
        row = {'startTime': '2026-10-08 00:03:53', 'meetingNumber': '10000000001',
               'meetingId': 'observed-occurrence', 'timezone': 'America/Los_Angeles'}
        record = recording_from_row(99, row)
        recording_file_metadata(self.file_payload(), record)
        self.assertNotIn('occurrence_id', public_record(record))
        with self.assertRaises(ZoomPending):
            recording_file_metadata(self.file_payload(meetingId='different-occurrence'), record)
        with self.assertRaises(ZoomPending):
            recording_file_metadata(self.file_payload(meetingNumber='different-meeting'), record)

    def test_file_dates_require_known_timezone_and_respect_daylight_saving(self):
        payload = self.file_payload()
        payload['recordingFiles'][0].update(recordingStart='2026-01-07 17:03:53', recordingEnd='2026-01-07 20:32:03')
        self.assertEqual(recording_file_metadata(payload)['files'][0]['started_at_utc'], '2026-01-08T01:03:53+00:00')
        with self.assertRaises(ZoomPending):
            recording_file_metadata(self.file_payload(timezone='unknown-local-label'))
        payload['recordingFiles'][0].update(recordingStart='2026-11-01 01:30:00', recordingEnd='2026-11-01 03:00:00')
        with self.assertRaisesRegex(ZoomPending, 'ambigua'):
            recording_file_metadata(payload)

    def test_file_metadata_rejects_untrusted_playback_and_invalid_time_range(self):
        payload = self.file_payload()
        payload['recordingFiles'][0]['playUrl'] = 'https://zoom.us.attacker.example/rec/play/video'
        with self.assertRaises(ZoomPending):
            recording_file_metadata(payload)
        payload = self.file_payload()
        payload['recordingFiles'][0]['recordingEnd'] = '2026-10-06 20:32:03'
        with self.assertRaises(ZoomPending):
            recording_file_metadata(payload)

    def test_utc_metadata_and_occurrence_uuid_distinguish_recurring_classes(self):
        row = {'startTime': '2026-10-08 00:03:53', 'meetingNumber': '10000000001',
               'meetingId': 'observed-occurrence', 'timezone': 'America/Los_Angeles',
               'listStartTime': '7 oct. 2026 5:03 p. m.'}
        first = recording_from_row(99, row)
        self.assertEqual(first['started_at_utc'], '2026-10-08T00:03:53+00:00')
        second = recording_from_row(99, {**row, 'meetingId': 'other-occurrence', 'startTime': '2026-09-30 00:00:00'})
        self.assertNotEqual(first['class_id'], second['class_id'])

    def test_only_exact_transcript_request_is_accepted(self):
        self.assertTrue(transcript_request('https://us02web.zoom.us/rec/play/vtt?type=transcript&fid=observed'))
        for url in ('https://zoom.us.attacker.example/rec/play/vtt?type=transcript',
                    'https://us02web.zoom.us/rec/play/vtt?type=cc',
                    'http://us02web.zoom.us/rec/play/vtt?type=transcript'):
            self.assertFalse(transcript_request(url))
        self.assertEqual(decode_body({'body': base64.b64encode(VTT.encode()).decode(), 'base64Encoded': True}), VTT)


if __name__ == '__main__':
    unittest.main()

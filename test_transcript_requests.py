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


class ZoomFixtures:
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


class TranscriptRequestTests(ZoomFixtures, unittest.TestCase):
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


class BrowserCaptureTests(ZoomFixtures, unittest.TestCase):
    """Capturas zoom-vtt-*.json: mismo id y referencia que un cURL del mismo visor."""

    def capture(self, folder, name='zoom-vtt-a.json', **changes):
        import hashlib
        from zoom_capture import viewer_reference
        data = {'schema': 'zoom-vtt-capture', 'version': 1,
                'id': hashlib.sha256(b'observed-fid').hexdigest()[:20],
                'recording_reference': viewer_reference(VIEWER),
                'captured_at': '2026-11-11T00:00:00Z', 'vtt': VTT}
        data.update(changes)
        path = Path(folder) / name
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_capture_matches_curl_identity_and_associates(self):
        from transcript_requests import import_captures
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder) / 'kb')
            self.import_one(ctx)
            report = import_captures(ctx, [str(self.capture(folder))])
            self.assertEqual(report[0]['status'], 'sin_cambios')
            self.assertEqual(len(list(ctx['root'].glob('transcripciones-sin-fecha/*'))), 1)
            source = load_json(next(ctx['root'].glob('transcripciones-sin-fecha/*/fuente.json')))
            self.assertEqual(source['acquisition'], 'browser_capture')
            self.metadata(ctx)
            self.assertEqual(associate_pending(ctx)[0]['status'], 'associated')

    def test_glob_and_duplicates_import_once(self):
        from transcript_requests import import_captures
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder) / 'kb')
            self.capture(folder, 'zoom-vtt-a.json'); self.capture(folder, 'zoom-vtt-a (1).json')
            report = import_captures(ctx, [str(Path(folder) / 'zoom-vtt-*.json')])
            self.assertEqual(len(report), 1)
            original = next(ctx['root'].glob('transcripciones-sin-fecha/*/fuentes/original.vtt'))
            self.assertEqual(original.read_bytes(), VTT.encode())

    def test_invalid_capture_rejects_whole_batch_before_writing(self):
        from transcript_requests import import_captures
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder) / 'kb')
            good = self.capture(folder)
            for bad in ({'vtt': '<html>login</html>'}, {'id': 'NOT-HEX'}, {'schema': 'otro'},
                        {'recording_reference': {'method': 'url', 'sha256': VIEWER}}):
                path = self.capture(folder, 'zoom-vtt-bad.json', **bad)
                with self.assertRaises(ValueError):
                    import_captures(ctx, [str(good), str(path)])
                self.assertFalse(ctx['root'].exists())

    def duplicate_setup(self, folder):
        """VTT importado a mano antes de los metadatos de Zoom: dos clases de la misma sesion."""
        ctx = context(Path(folder) / 'kb')
        original = Path(folder) / 'manual.vtt'
        original.write_bytes(VTT.encode())
        manual = import_vtt(ctx, original, started_at='2026-10-07T19:03:53-05:00', meeting_id='123456789')
        manual_folder = next(p.parent for p in ctx['root'].glob('semanas/*/clases/*/clase.json')
                             if load_json(p)['id'] == manual['class_id'])
        processed = manual_folder / 'procesado' / 'transcripciones' / 'x' / 'y' / 'z'
        write_json(processed / 'estado.json', {'status': 'partial'})
        zoom_folder, zoom = self.metadata(ctx)
        write_json(ctx['root'] / 'grabaciones.json', [{'class_id': zoom['id'], 'status': 'metadata_captured',
                                                       'transcript_status': 'pending_import'}])
        return ctx, manual['class_id'], manual_folder, zoom['id'], zoom_folder

    def test_merge_manual_duplicate_keeps_originals_and_capture_becomes_provenance(self):
        from manual_transcripts import merge_classes
        from transcript_requests import import_captures
        with tempfile.TemporaryDirectory() as folder:
            ctx, manual_id, manual_folder, zoom_id, zoom_folder = self.duplicate_setup(folder)
            self.assertIn('posible duplicado', ' '.join(organize(ctx, dry_run=True)['pending']))
            import_captures(ctx, [str(self.capture(folder))])
            self.assertIn('asociacion_existente', associate_pending(ctx)[0]['reason'])
            self.assertEqual(merge_classes(ctx, manual_id, zoom_id, dry_run=True)['status'], 'preview')
            self.assertTrue((manual_folder / 'clase.json').exists())
            merge_classes(ctx, manual_id, zoom_id)
            self.assertFalse((manual_folder / 'clase.json').exists())
            self.assertEqual(load_json(manual_folder / 'clase.unificada.json')['merged_into_class_id'], zoom_id)
            report = associate_pending(ctx)
            self.assertEqual(report[0]['status'], 'duplicate')
            row = load_json(zoom_folder / 'clase.json')
            self.assertEqual(len(row['transcripts']), 1)
            entry = row['transcripts'][0]
            self.assertEqual(entry['acquisition'], 'manual_vtt')
            self.assertTrue(entry['path'].startswith(manual_folder.relative_to(ctx['root']).as_posix()))
            self.assertEqual(entry['processing_manifest_path'],
                             (manual_folder / 'clase.unificada.json').relative_to(ctx['root']).as_posix())
            self.assertEqual(entry['recording_reference']['method'], 'observed_viewer_path_sha256')
            self.assertEqual(len(entry['verified_by']), 1)
            self.assertEqual(load_json(ctx['root'] / 'grabaciones.json')[0]['transcript_status'], 'imported')
            self.assertEqual(associate_pending(ctx), [])
            summary = organize(ctx, dry_run=True)
            self.assertEqual(summary['classes'], 1)
            self.assertEqual(summary['unassigned_transcripts'], 0)
            self.assertNotIn('posible duplicado', ' '.join(summary['pending']))
            self.assertTrue((manual_folder / 'procesado' / 'transcripciones' / 'x' / 'y' / 'z' / 'estado.json').exists())

    def test_merge_refuses_zoom_source_or_different_session(self):
        from manual_transcripts import merge_classes
        with tempfile.TemporaryDirectory() as folder:
            ctx, manual_id, manual_folder, zoom_id, _ = self.duplicate_setup(folder)
            with self.assertRaisesRegex(ValueError, 'metadatos de Zoom'):
                merge_classes(ctx, zoom_id, manual_id)
            row = load_json(manual_folder / 'clase.json')
            row['started_at_utc'] = '2026-10-09T00:03:53+00:00'
            write_json(manual_folder / 'clase.json', row)
            with self.assertRaisesRegex(ValueError, 'no comparten'):
                merge_classes(ctx, manual_id, zoom_id)
            self.assertTrue((manual_folder / 'clase.json').exists())

    def inbox_setup(self, folder):
        from transcript_requests import init_capture_inbox
        config = {'knowledge_root': str(Path(folder))}
        ctx = context(Path(folder) / 'tmp')
        ctx['root'] = Path(folder) / '2026-02' / 'agentes-99'
        write_json(ctx['root'] / 'curso.json', {'course_id': 99})
        self.metadata(ctx)
        init_capture_inbox(config)
        return config, ctx, Path(folder) / 'entrada-capturas'

    def test_inbox_routes_capture_to_course_by_viewer_and_files_it_away(self):
        from transcript_requests import import_inbox
        with tempfile.TemporaryDirectory() as folder:
            config, ctx, inbox = self.inbox_setup(folder)
            self.capture(inbox, 'zoom-vtt-a.json')
            self.capture(inbox, 'zoom-vtt-a (1).json')
            self.capture(inbox, 'zoom-vtt-otro.json', id='b' * 20,
                         recording_reference={'method': 'observed_viewer_path_sha256', 'sha256': 'c' * 64})
            (inbox / 'zoom-vtt-roto.json').write_text('{"schema": "x"}', encoding='utf-8')
            (inbox / 'notas.txt').write_text('no es captura', encoding='utf-8')
            with patch('knowledge_core.course_context', return_value=ctx):
                preview = import_inbox(config, dry_run=True)
                self.assertTrue((inbox / 'zoom-vtt-a.json').exists())
                self.assertFalse((inbox / 'rechazadas' / 'zoom-vtt-roto.json').exists())
                report = {r['file']: r for r in import_inbox(config)}
            self.assertEqual(len(preview), 4)
            self.assertEqual(report['zoom-vtt-a.json']['association'], 'associated')
            self.assertEqual(report['zoom-vtt-a.json']['course_id'], 99)
            self.assertEqual(report['zoom-vtt-otro.json']['reason'], 'sin_metadatos_zoom')
            self.assertEqual(report['zoom-vtt-roto.json']['status'], 'rejected')
            self.assertEqual(sorted(p.name for p in inbox.iterdir() if p.is_file()),
                             ['.usil-zoom-capture-inbox', 'notas.txt', 'zoom-vtt-otro.json'])
            self.assertEqual(len(list((inbox / 'importadas').iterdir())), 2)
            self.assertTrue((inbox / 'rechazadas' / 'zoom-vtt-roto.json').exists())
            row = next(load_json(p) for p in ctx['root'].glob('semanas/*/clases/*/clase.json'))
            self.assertEqual(row['transcripts'][0]['acquisition'], 'browser_capture')

    def v2_capture(self, folder, name='zoom-vtt-v2.json', **recording):
        import hashlib
        rec = {'occurrence_sha256': hashlib.sha256(b'observed-occurrence').hexdigest(),
               'file_id_sha256': hashlib.sha256(b'observed-file').hexdigest(), 'file_type': 'MP4',
               'started_at_utc': '2026-10-08T00:03:53.000Z', 'duration_seconds': 10800,
               'topic': 'Agentes - 2026-02'}
        rec.update(recording)
        return self.capture(folder, name, version=2, recording=rec)

    def v2_setup(self, folder, with_metadata):
        from transcript_requests import init_capture_inbox
        config = {'knowledge_root': str(Path(folder)), 'root': str(Path(folder) / 'lib')}
        write_json(Path(folder) / 'lib' / 'cursos.json', [{'id': 99, 'name': 'Agentes - 2026-02'},
                                                           {'id': 100, 'name': 'Otro - 2026-02'}])
        ctx = context(Path(folder) / 'tmp')
        ctx['root'] = Path(folder) / '2026-02' / 'agentes-99'
        write_json(ctx['root'] / 'curso.json', {'course_id': 99})
        if with_metadata:
            self.metadata(ctx)
        init_capture_inbox(config)
        return config, ctx, Path(folder) / 'entrada-capturas'

    def test_v2_capture_creates_class_routed_by_course_name(self):
        from transcript_requests import import_inbox
        with tempfile.TemporaryDirectory() as folder:
            config, ctx, inbox = self.v2_setup(folder, with_metadata=False)
            self.v2_capture(inbox)
            self.v2_capture(inbox, 'zoom-vtt-otro.json', id='d' * 20, topic='Curso desconocido')
            with patch('knowledge_core.course_context', return_value=ctx):
                report = {r['file']: r for r in import_inbox(config)}
            self.assertEqual(report['zoom-vtt-v2.json']['association'], 'associated')
            self.assertEqual(report['zoom-vtt-otro.json']['reason'], 'sin_metadatos_zoom')
            rows = [load_json(p) for p in ctx['root'].glob('semanas/*/clases/*/clase.json')]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            import hashlib
            self.assertEqual(row['id'], hashlib.sha256(b'observed-occurrence').hexdigest()[:20])
            self.assertEqual(row['started_at_utc'], '2026-10-08T00:03:53+00:00')
            self.assertEqual(row['transcripts'][0]['acquisition'], 'browser_capture')
            self.assertEqual(row['zoom_detail']['files'][0]['ended_at_utc'], '2026-10-08T03:03:53+00:00')
            inventory = load_json(ctx['root'] / 'grabaciones.json')
            self.assertEqual([(r['class_id'], r['transcript_status']) for r in inventory], [(row['id'], 'imported')])

    def test_v2_capture_for_known_class_does_not_duplicate(self):
        from transcript_requests import import_inbox
        with tempfile.TemporaryDirectory() as folder:
            config, ctx, inbox = self.v2_setup(folder, with_metadata=True)
            before = load_json(next(ctx['root'].glob('semanas/*/clases/*/clase.json')))
            self.v2_capture(inbox)
            with patch('knowledge_core.course_context', return_value=ctx):
                report = import_inbox(config)
            self.assertEqual(report[0]['association'], 'associated')
            rows = [load_json(p) for p in ctx['root'].glob('semanas/*/clases/*/clase.json')]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['meeting_id'], before['meeting_id'])
            self.assertEqual(len(rows[0]['zoom_detail']['files']), 1)
            self.assertEqual(rows[0]['zoom_detail']['source_timezone'], 'America/Los_Angeles')

    def test_v2_rejects_tampered_recording(self):
        from transcript_requests import load_capture
        with tempfile.TemporaryDirectory() as folder:
            for bad in ({'occurrence_sha256': 'x'}, {'started_at_utc': '2026-10-08 00:03'}, {'duration_seconds': -5},
                        {'file_type': 'EXE'}):
                with self.assertRaises(ValueError):
                    load_capture(self.v2_capture(folder, 'bad.json', **bad))

    def test_inbox_requires_marker(self):
        from transcript_requests import import_inbox
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'init-capture-inbox'):
                import_inbox({'knowledge_root': folder})

    def test_conflicting_captures_with_same_id_are_rejected(self):
        from transcript_requests import import_captures
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(Path(folder) / 'kb')
            a = self.capture(folder, 'a.json')
            b = self.capture(folder, 'b.json', vtt=VTT.replace('neuron', 'cambio'))
            with self.assertRaises(ValueError):
                import_captures(ctx, [str(a), str(b)])


if __name__ == '__main__':
    unittest.main()

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bazarr_sync.alignment import AlignmentResult
from bazarr_sync.benchmark import _baseline, _measure, _write_output, main, synthetic_fixture
from bazarr_sync.evaluation import (compare_reviewed, media_fingerprint, preserved_content,
                                    reference_coverage, reference_template, validate_reference)
from bazarr_sync.subtitles import parse_subtitle


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.document, self.words, self.truth = synthetic_fixture(lambda t: t + 3)
        self.media = {'sha256': 'a' * 64, 'size_bytes': 42}
        self.reference = reference_template(self.document, self.media, 2, 3)

    def reviewed(self):
        reference = copy.deepcopy(self.reference)
        reference['independently_reviewed'] = True
        for row in reference['annotations']:
            row['start_seconds'], row['end_seconds'] = self.truth[row['cue_index']]
        return reference

    def test_template_has_no_invented_truth_and_spans_media(self):
        self.assertFalse(self.reference['independently_reviewed'])
        self.assertEqual([a['cue_index'] for a in self.reference['annotations']], [0, 7, 15])
        self.assertTrue(all(a['start_seconds'] is None and a['end_seconds'] is None
                            for a in self.reference['annotations']))
        with self.assertRaisesRegex(ValueError, 'human review'):
            validate_reference(self.document, self.reference, self.media, 2)
        reference = self.reviewed()
        annotations = validate_reference(self.document, reference, self.media, 2)
        coverage = reference_coverage(self.document, annotations)
        self.assertEqual(coverage['source_time_span_fraction'], 1)
        self.assertEqual(coverage['source_time_thirds'], {'early': 1, 'middle': 1, 'late': 1})
        self.assertFalse(coverage['release_gate_established'])

    def test_stale_reference_wrong_media_or_audio_rejected(self):
        reference = self.reviewed()
        for changes, media, audio in [({'subtitle_sha256': 'b' * 64}, self.media, 2),
                                      ({'format': 'ass'}, self.media, 2),
                                      ({'cue_count': 12}, self.media, 2),
                                      ({}, dict(self.media, size_bytes=43), 2),
                                      ({}, self.media, 1),
                                      ({'audio_index': True}, self.media, True)]:
            with self.subTest(changes=changes, audio=audio), self.assertRaises(ValueError):
                validate_reference(self.document, dict(reference, **changes), media, audio)

    def test_malformed_annotations_fail_instead_of_distorting_scores(self):
        for changes in [{'cue_index': True}, {'cue_index': -1}, {'cue_index': 16},
                        {'text': 'changed'}, {'source_start_seconds': 9},
                        {'start_seconds': True}, {'start_seconds': '12'},
                        {'start_seconds': float('nan')}, {'end_seconds': float('inf')},
                        {'start_seconds': -1}, {'start_seconds': 99, 'end_seconds': 12}]:
            reference = self.reviewed()
            reference['annotations'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_reference(self.document, reference, self.media, 2)
        reference = self.reviewed()
        reference['annotations'][1] = reference['annotations'][0]
        with self.assertRaisesRegex(ValueError, 'unique'):
            validate_reference(self.document, reference, self.media, 2)
        reference = dict(self.reference, independently_reviewed=True)
        with self.assertRaisesRegex(ValueError, 'no reviewed timestamps'):
            validate_reference(self.document, reference, self.media, 2)

    def test_partial_reference_counts_only_reviewed_endpoints(self):
        reference = self.reviewed()
        reference['annotations'][0]['end_seconds'] = None
        reference['annotations'][1]['start_seconds'] = None
        reference['annotations'][2].update(start_seconds=None, end_seconds=None)
        annotations = validate_reference(self.document, reference, self.media, 2)
        row = _measure(self.document, self.words, annotations=annotations)
        self.assertEqual(row['reviewed_timing']['before']['starts']['count'], 1)
        self.assertEqual(row['reviewed_timing']['before']['ends']['count'], 1)
        self.assertEqual(row['reviewed_timing']['before']['all_reviewed_endpoints']['mean_absolute_error_seconds'], 3)
        self.assertLess(row['reviewed_timing']['after']['all_reviewed_endpoints']['maximum_absolute_error_seconds'], .002)
        self.assertEqual(reference_coverage(self.document, annotations)['annotated_cue_count'], 2)

    def test_abstention_reports_unchanged_effective_output(self):
        annotations = validate_reference(self.document, self.reviewed(), self.media, 2)
        row = _measure(self.document, [], annotations=annotations)
        self.assertFalse(row['proposal_available'])
        self.assertEqual(row['effective_output'], 'unchanged_input_on_abstention')
        self.assertEqual(row['reviewed_timing']['before'], row['reviewed_timing']['after'])

    def test_harmful_changes_include_endpoints_and_count_each_cue_once(self):
        document, _, truth = synthetic_fixture()
        timings = [(s + 1, e + 1) for s, e in truth]
        timings[1] = (truth[1][0], truth[1][1] + 1)
        output = parse_subtitle(document.render(timings))
        annotations = [{'cue_index': i, 'start_seconds': s, 'end_seconds': e}
                       for i, (s, e) in enumerate(truth[:2])]
        score = compare_reviewed(document, output, annotations)
        self.assertEqual(score['diagnostic_harmful_cue_count'], 2)
        self.assertEqual(score['after']['all_reviewed_endpoints']['p95_absolute_error_seconds'], 1)
        self.assertFalse(score['thresholds_are_release_criteria'])

    def test_scores_use_serialized_precision_and_do_not_feed_truth_to_fitting(self):
        annotation = [{'cue_index': 0, 'start_seconds': 15.001, 'end_seconds': None}]
        timings = [(c.start + .0006, c.end + .0006) for c in self.document.cues]
        with patch('bazarr_sync.benchmark.align', return_value=AlignmentResult('preview', 'test', timings)) as fit:
            row = _measure(self.document, self.words, annotations=annotation)
        fit.assert_called_once_with(self.document, self.words)
        self.assertEqual(row['reviewed_timing']['after']['starts']['maximum_absolute_error_seconds'], 0)

    def test_preservation_rejects_text_order_style_and_encoding_changes(self):
        source = b'[Script Info]\nTitle: Keep\n[Events]\nFormat: Start, End, Text\nDialogue: 0:00:10.00,0:00:12.00,{\\i1}Hello\n'
        for format in ('ass', 'ssa'):
            document = parse_subtitle(source, format)
            shifted = parse_subtitle(document.render([(11, 13)]), format)
            self.assertTrue(preserved_content(document, shifted))
            changed = parse_subtitle(shifted.raw.replace(b'Title: Keep', b'Title: Changed'), format)
            self.assertFalse(compare_reviewed(document, changed, [])['comparable'])
        changed = parse_subtitle(self.document.raw.replace(b'copper kettle', b'copper bottle'))
        self.assertFalse(compare_reviewed(self.document, changed, [])['comparable'])
        changed = parse_subtitle(b'\xef\xbb\xbf' + self.document.raw)
        self.assertFalse(preserved_content(self.document, changed))


class BenchmarkCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.document, self.words, self.truth = synthetic_fixture(lambda t: t + 3)
        self.subtitle = self.root / 'source.srt'
        self.subtitle.write_bytes(self.document.raw)
        self.media = self.root / 'media.bin'
        self.media.write_bytes(b'fake media for testing reference identity only')
        self.transcript = self.root / 'transcript.json'
        self.transcript.write_text(json.dumps(self.words))
        self.reference = self.root / 'reference.json'
        self.common = ['--subtitle', str(self.subtitle), '--media', str(self.media), '--audio-index', '2']

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_create_review_score_roundtrip_leaves_all_inputs_intact(self):
        self.assertEqual(main(self.common + ['--make-reference-template', '--sample-count', '3',
                                             '--output', str(self.reference)]), 0)
        reference = json.loads(self.reference.read_text())
        reference['independently_reviewed'] = True
        for row in reference['annotations']:
            row['start_seconds'], row['end_seconds'] = self.truth[row['cue_index']]
        self.reference.write_text(json.dumps(reference))
        inputs = {path: path.read_bytes() for path in (self.subtitle, self.media, self.transcript, self.reference)}
        output = self.root / 'report.json'
        self.assertEqual(main(self.common + ['--transcript', str(self.transcript),
                                             '--timing-reference', str(self.reference), '--output', str(output)]), 0)
        report = json.loads(output.read_text())
        self.assertEqual(report['timing_reference']['verification'], 'user-attestation-only')
        self.assertFalse(report['timing_reference']['used_for_model_fitting'])
        self.assertEqual(report['results'][0]['reviewed_timing']['after']['starts']['count'], 3)
        for path, raw in inputs.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_output_never_overwrites_existing_inputs_or_reports(self):
        for destination in (self.subtitle, self.transcript, self.media):
            original = destination.read_bytes()
            with self.assertRaises(ValueError):
                _write_output(destination, '{}', [destination])
            self.assertEqual(destination.read_bytes(), original)
        output = self.root / 'report.json'
        output.write_text('existing')
        with self.assertRaises(FileExistsError):
            _write_output(output, '{}', [])
        self.assertEqual(output.read_text(), 'existing')
        alias = self.root / 'alias.json'
        os.link(self.subtitle, alias)
        with self.assertRaises(FileExistsError):
            _write_output(alias, '{}', [self.subtitle])
        self.assertEqual(self.subtitle.read_bytes(), self.document.raw)

    def test_media_content_change_invalidates_reference_even_at_same_size(self):
        identity = media_fingerprint(self.media)
        self.media.write_bytes(b'x' * self.media.stat().st_size)
        self.assertNotEqual(media_fingerprint(self.media), identity)

    def test_transcript_cache_identity_must_match_reviewed_media_and_audio(self):
        reference = reference_template(self.document, media_fingerprint(self.media), 2, 1)
        reference['independently_reviewed'] = True
        reference['annotations'][0]['start_seconds'] = 12.
        self.reference.write_text(json.dumps(reference))
        identity = {'media_sha256': media_fingerprint(self.media)['sha256'],
                    'audio_index': 2, 'backend': 'test-local-backend'}
        arguments = self.common + ['--transcript', str(self.transcript), '--timing-reference', str(self.reference)]
        for changes in ({'audio_index': 1}, {'media_sha256': 'b' * 64}, {'audio_index': True}):
            self.transcript.write_text(json.dumps({'transcript': self.words, 'identity': dict(identity, **changes)}))
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Transcript identity'):
                main(arguments)
        self.transcript.write_text(json.dumps({'transcript': self.words, 'identity': identity}))
        output = self.root / 'cache-report.json'
        main(arguments + ['--output', str(output)])
        report = json.loads(output.read_text())
        self.assertEqual(report['inputs']['subtitle_sha256'], self.document.sha256)
        self.assertEqual(report['inputs']['transcript_identity'], identity)
        self.assertEqual(len(report['timing_reference']['artifact_sha256']), 64)

    def test_baseline_uses_scratch_input_selected_stream_and_recorded_profile(self):
        args = SimpleNamespace(ffsubsync='fake-ffsubsync', media=str(self.media), audio_index=2,
                               ffsubsync_arg=['--no-fix-framerate', '--gss', '--max-offset-seconds=600'])
        def baseline(command, **kwargs):
            source = Path(command[command.index('-i') + 1])
            output = Path(command[command.index('-o') + 1])
            self.assertNotEqual(source, self.subtitle)
            self.assertEqual(source.read_bytes(), self.document.raw)
            output.write_bytes(source.read_bytes().replace(b'copper kettle', b'copper bottle'))
            source.write_bytes(b'baseline accidentally rewrote scratch input')
            self.assertIn('--reference-stream=0:2', command)
            self.assertIn('--vad=webrtc', command)
            return SimpleNamespace(returncode=0, stderr=b'')
        annotations = [{'cue_index': 0, 'start_seconds': 12., 'end_seconds': None}]
        with patch('bazarr_sync.benchmark.subprocess.run', side_effect=baseline):
            row = _baseline(self.document, args, self.truth, annotations)
        self.assertFalse(row['preserved_non_timing_content'])
        self.assertFalse(row['reviewed_timing']['comparable'])
        self.assertFalse(row['after']['comparable'])
        self.assertEqual(self.subtitle.read_bytes(), self.document.raw)
        self.assertIn('--max-offset-seconds=600', row['arguments'])

    def test_conflicting_or_unsafe_cli_options_fail_before_processing(self):
        cases = [self.common + ['--make-reference-template', '--transcript', str(self.transcript)],
                 ['--synthetic', '--make-reference-template'],
                 ['--subtitle', str(self.subtitle), '--make-reference-template'],
                 self.common + ['--transcript', str(self.transcript), '--reference', 'a', '--timing-reference', 'b'],
                 ['--synthetic', '--ffsubsync-arg=--gss']]
        for flag in ('--srtout=source.srt', '--overwrite-input', '--reference-stream=0:9', '--vad', '--gss=true'):
            cases.append(self.common + ['--transcript', str(self.transcript), '--ffsubsync', 'fake', '--ffsubsync-arg=' + flag])
        for arguments in cases:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(arguments)
            self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

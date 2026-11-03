import hashlib
from pathlib import Path
import tempfile
import unittest

from bazarr_sync.alignment import align, manual_preview
from bazarr_sync.benchmark import synthetic_fixture
from bazarr_sync.files import apply_result, rollback
from bazarr_sync.inference import choose_audio_track, media_duration
from bazarr_sync.subtitles import parse_subtitle


class PreservationTests(unittest.TestCase):
    def test_exact_identity_encodings(self):
        source = '1\r\n00:00:10.000 --> 00:00:12,000  X1:10\r\n<i>Hello café!</i>\r\n\r\n'
        for encoding, bom in [('utf-8', b''), ('utf-8', b'\xef\xbb\xbf'), ('utf-16-be', b'\xfe\xff'), ('utf-16-le', b'\xff\xfe'), ('cp1252', b'')]:
            with self.subTest(encoding=encoding, bom=bom):
                raw = bom + source.encode(encoding)
                document = parse_subtitle(raw)
                self.assertEqual(raw, document.render([(c.start, c.end) for c in document.cues]))
                moved = document.render([(11., 13.)])
                reparsed = parse_subtitle(moved)
                self.assertEqual(reparsed.cues[0].text, '<i>Hello café!</i>')
                self.assertTrue(moved.startswith(bom))

    def test_ass_styles_overlaps_and_tag_guards(self):
        source = b'[Script Info]\r\nTitle: Keep exactly\r\n[V4+ Styles]\r\nStyle: noisy,unchanged\r\n[Events]\r\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\nDialogue: 0,0:00:10.00,0:00:12.00,Default,Fred,0,0,0,,{\\k20}Some, text\\NMore\r\nComment: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,untouched\r\n'
        document = parse_subtitle(source, 'ass')
        self.assertEqual(document.render([(10, 12)]), source)
        shifted = document.render([(11, 13)])
        self.assertIn(b'{\\k20}Some, text\\NMore', shifted)
        self.assertIn(b'Comment: 0,0:00:10.00,0:00:12.00', shifted)
        with self.assertRaises(ValueError):
            document.render([(11, 14)])

    def test_malformed_and_precision(self):
        for raw in [b'1\n00:99:10,000 --> 00:00:12,000\nText', b'1\n00:00:10 --> 00:00:12\nText', b'no cues']:
            with self.assertRaises(ValueError):
                parse_subtitle(raw)
        document = parse_subtitle(b'1\n00:00:10,000 --> 00:00:12,000\nText')
        with self.assertRaises(ValueError):
            document.render([(1, 1.00001)])


class AlignmentTests(unittest.TestCase):
    def test_known_reference_with_independent_holdout(self):
        for transform, status in [(lambda t:t, 'unchanged'), (lambda t:t+3, 'preview'), (lambda t:(t-2)/1.03, 'preview')]:
            document, words, reference = synthetic_fixture(transform)
            result = align(document, words)
            self.assertEqual(result.status, status, result.reason)
            self.assertGreater(result.report['holdout_count'], 0)
            for proposal, truth in zip(result.timings, reference):
                self.assertLess(abs(proposal[0] - truth[0]), .002)
                self.assertLess(abs(proposal[1] - truth[1]), .002)

    def test_edit_jump_and_wrong_reference_abstain(self):
        document, words, _ = synthetic_fixture(lambda t:t+(8 if t>=125 else 0))
        self.assertEqual(align(document, words).status, 'uncertain')
        self.assertEqual(align(document, [dict(w,text='unrelated') for w in words]).status, 'uncertain')

    def test_repeated_ambiguous_dialogue_abstains(self):
        document, words, _ = synthetic_fixture()
        repeated_source = document.source
        for cue in document.cues:
            repeated_source = repeated_source.replace(cue.text, 'Please come over here now')
        repeated_document = parse_subtitle(repeated_source.encode())
        repeated = []
        for cue in repeated_document.cues:
            for index, token in enumerate(cue.text.split()):
                repeated.append({'start':cue.start+index*.2,'end':cue.start+(index+1)*.2,'text':token})
        result = align(repeated_document, repeated)
        self.assertEqual(result.status, 'uncertain')
        self.assertEqual(result.report['matched_anchors'], [])

    def test_manual_preview_validation_and_explicit_status(self):
        document, _, _ = synthetic_fixture()
        result = manual_preview(document, [{'cue_index':0,'time':14.}])
        self.assertEqual(result.status, 'preview')
        self.assertTrue(result.report['requires_apply'])
        self.assertEqual(result.timings[0], (14.,18.))
        for anchors in [[{'cue_index':-1,'time':4}], [{'cue_index':2,'time':50},{'cue_index':1,'time':60}], [{'cue_index':0,'time':float('nan')}], [{'cue_index':0,'time':10},{'cue_index':2,'time':9}]]:
            with self.assertRaises(ValueError):
                manual_preview(document,anchors)
        with self.assertRaises(ValueError):
            manual_preview(document,[{'cue_index':0,'time':14}], media_duration=20)

    def test_audio_ambiguity(self):
        probe={'streams':[{'index':1,'codec_type':'audio','tags':{'language':'eng'}}, {'index':2,'codec_type':'audio','tags':{'language':'eng','title':'Commentary'}}]}
        self.assertEqual(choose_audio_track(probe),1)
        self.assertEqual(choose_audio_track(probe,2),2)
        probe['streams'].append({'index':3,'codec_type':'audio','tags':{'language':'eng'}})
        with self.assertRaises(ValueError):
            choose_audio_track(probe)
        with self.assertRaises(ValueError):
            choose_audio_track(probe,0)
        self.assertIsNone(media_duration({'format':{'duration':'NaN'}}))


class FileSafetyTests(unittest.TestCase):
    def test_backup_rollback_and_stale_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'subtitle.srt'
            path.write_bytes(b'original')
            expected=hashlib.sha256(b'original').hexdigest()
            record=apply_result(path,b'corrected',expected,Path(temporary)/'backups')
            self.assertEqual(Path(record['backup']).read_bytes(), b'original')
            self.assertEqual(path.read_bytes(),b'corrected')
            rollback(record)
            self.assertEqual(path.read_bytes(),b'original')
            with self.assertRaises(ValueError):
                rollback(record)
            path.write_bytes(b'new download')
            with self.assertRaises(ValueError):
                apply_result(path,b'bad',expected,Path(temporary)/'backups')
            self.assertEqual(path.read_bytes(),b'new download')

    def test_lock_and_backup_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'subtitle.srt'
            path.write_bytes(b'original')
            lock=path.with_name('.subtitle.srt.bazarr-sync.lock')
            lock.write_text('other worker')
            with self.assertRaises(ValueError):
                apply_result(path,b'corrected',hashlib.sha256(b'original').hexdigest(),Path(temporary)/'backups')
            self.assertTrue(lock.exists())
            self.assertEqual(path.read_bytes(),b'original')
            lock.unlink()
            record=apply_result(path,b'corrected',hashlib.sha256(b'original').hexdigest(),Path(temporary)/'backups')
            Path(record['backup']).write_bytes(b'tampered backup')
            with self.assertRaises(ValueError):
                rollback(record)
            self.assertEqual(path.read_bytes(),b'corrected')


if __name__=='__main__':
    unittest.main()

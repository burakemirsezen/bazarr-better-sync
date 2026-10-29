"""Reproducible scratch-only alignment experiments; no source publication.

Run: python -m bazarr_sync.benchmark --synthetic --output report.json
Real: --subtitle input.srt --transcript words.json --reference trusted.srt
      [--media input.mkv --ffsubsync ffsubsync]
References must be independently reviewed; an existing subtitle is not truth.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
import tracemalloc
from .alignment import align
from .subtitles import load_subtitle, parse_subtitle


SENTENCES = [
    'The copper kettle whistles beside the kitchen window',
    'My sister mailed a postcard from the snowy mountains',
    'Our ancient telescope reveals bright galaxies beyond Jupiter',
    'Please fetch your yellow umbrella before leaving tomorrow',
    'A curious fox wandered through the abandoned railway station',
    'Those velvet curtains hide a surprisingly narrow doorway',
    'Seven little boats sailed across the peaceful harbor',
    'Fresh cinnamon bread arrived early this rainy morning',
    'Her silver necklace disappeared during the crowded festival',
    'We discovered unusual footprints beneath the garden bridge',
    'Every musician remembered their first nervous concert performance',
    'The exhausted detective examined each mysterious handwritten letter',
    'Someone painted enormous butterflies above our bedroom ceiling',
    'Your grandfather repaired these broken wooden chairs yesterday',
    'Several friendly neighbors gathered outside the village bakery',
    'That purple bicycle belongs to an adventurous young scientist',
]


def synthetic_fixture(transform=lambda t: t):
    from .subtitles import _timestamp
    cues, words, reference = [], [], []
    for index, sentence in enumerate(SENTENCES):
        start = 12. + 15 * index
        end = start + 4.
        cues.append(f'{index+1}\n{_timestamp(transform(start),False)} --> {_timestamp(transform(end),False)}\n{sentence}\n\n')
        reference.append((start, end))
        tokens = sentence.split()
        for position, token in enumerate(tokens):
            words.append({'start': start + position * 3. / len(tokens),
                          'end': start + (position + 1) * 3. / len(tokens), 'text': token})
    return parse_subtitle(''.join(cues).encode(), 'srt'), words, reference


def timing_metrics(timings, reference):
    if len(timings) != len(reference):
        return {'comparable': False, 'reason': 'Reference cue count differs; explicit correspondence required'}
    errors = [abs(value - target) for pair, truth in zip(timings, reference) for value, target in zip(pair, truth)]
    starts = [abs(pair[0] - truth[0]) for pair, truth in zip(timings, reference)]
    return {'comparable': True, 'mean_error_seconds': statistics.mean(errors),
            'median_start_error_seconds': statistics.median(starts),
            'max_error_seconds': max(errors), 'cue_count': len(reference)}


def _measure(document, transcript, reference=None):
    tracemalloc.start()
    begin = time.perf_counter()
    result = align(document, transcript)
    elapsed = time.perf_counter() - begin
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    timings = result.timings or [(c.start, c.end) for c in document.cues]
    row = {'candidate': 'text-anchor-global-affine', 'status': result.status, 'reason': result.reason,
           'runtime_seconds': elapsed, 'peak_python_allocated_bytes': peak,
           'peak_process_rss_bytes': None, 'report': result.report,
           'source_modified': False}
    if reference is not None:
        row['before'] = timing_metrics([(c.start, c.end) for c in document.cues], reference)
        row['after'] = timing_metrics(timings, reference)
        row['harmful_changes'] = sum(abs(c.start - truth[0]) < .1 and abs(t[0] - truth[0]) > .5
                                    for c, t, truth in zip(document.cues, timings, reference))
    return row


def synthetic_benchmark():
    cases = {
        'already_aligned': lambda t: t,
        'constant_delay': lambda t: t + 3.,
        'progressive_drift': lambda t: (t - 2.) / 1.03,
        'abrupt_edit': lambda t: t + (8. if t >= 125. else 0.),
    }
    rows = []
    for name, transform in cases.items():
        document, words, reference = synthetic_fixture(transform)
        row = _measure(document, words, reference)
        row['case'] = name
        rows.append(row)
    document, words, reference = synthetic_fixture()
    wrong = [dict(w, text='unrelated') for w in words]
    row = _measure(document, wrong, reference)
    row['case'] = 'wrong_audio_transcript'
    rows.append(row)
    return {'schema': 1, 'synthetic': True, 'quality_claim': False,
            'limitation': 'Synthetic word timestamps do not test ASR or establish improvement on real failures',
            'python': platform.python_version(), 'platform': platform.platform(), 'results': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--subtitle')
    parser.add_argument('--transcript')
    parser.add_argument('--reference')
    parser.add_argument('--media')
    parser.add_argument('--ffsubsync', help='Baseline executable (writes only a temporary output)')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    if args.synthetic:
        report = synthetic_benchmark()
    else:
        if not args.subtitle or not args.transcript:
            parser.error('--subtitle and --transcript are required without --synthetic')
        document = load_subtitle(args.subtitle)
        transcript = json.loads(Path(args.transcript).read_text(encoding='utf-8'))
        if isinstance(transcript, dict):
            transcript = transcript['transcript']
        reference_document = load_subtitle(args.reference) if args.reference else None
        reference = [(c.start, c.end) for c in reference_document.cues] if reference_document else None
        if reference_document and [c.text for c in document.cues] != [c.text for c in reference_document.cues]:
            parser.error('Reference text/order differs; provide an independently mapped cue reference')
        report = {'schema': 1, 'synthetic': False, 'quality_claim': False,
                  'reference_independently_verified': False,
                  'results': [_measure(document, transcript, reference)]}
        if args.ffsubsync:
            if not args.media:
                parser.error('--media is required for ffsubsync baseline')
            with tempfile.TemporaryDirectory(prefix='bazarr-benchmark-') as temporary:
                output = Path(temporary) / ('baseline' + Path(args.subtitle).suffix)
                command = [args.ffsubsync, args.media, '-i', args.subtitle, '-o', str(output)]
                begin = time.perf_counter()
                completed = subprocess.run(command, capture_output=True, timeout=21600)
                row = {'candidate': 'ffsubsync', 'exit_code': completed.returncode,
                       'runtime_seconds': time.perf_counter() - begin,
                       'peak_process_rss_bytes': None, 'source_modified': False}
                if completed.returncode == 0 and output.is_file():
                    baseline = load_subtitle(output)
                    row['preserved_cue_text'] = [c.text for c in baseline.cues] == [c.text for c in document.cues]
                    if reference is not None:
                        row['after'] = timing_metrics([(c.start, c.end) for c in baseline.cues], reference)
                else:
                    row['stderr_tail'] = completed.stderr.decode(errors='replace')[-2000:]
                report['results'].append(row)
    serialized = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(serialized + '\n', encoding='utf-8')
    else:
        print(serialized)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

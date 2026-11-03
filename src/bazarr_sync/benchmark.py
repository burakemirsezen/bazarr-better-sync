"""Reproducible scratch-only alignment experiments; no source publication.

Run: python -m bazarr_sync.benchmark --synthetic --output report.json
Real: --subtitle input.srt --transcript words.json --reference trusted.srt
      [--media input.mkv --ffsubsync ffsubsync]
Review: --subtitle input.srt --media input.mkv --audio-index 1
        --make-reference-template --output private/reference.json
Score: --subtitle input.srt --media input.mkv --audio-index 1
       --transcript words.json --timing-reference private/reference.json
References must be independently reviewed; an existing subtitle is not truth.
"""
from __future__ import annotations

import argparse
import hashlib
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
from .evaluation import (compare_reviewed, media_fingerprint, preserved_content,
                         reference_coverage, reference_template, validate_reference)


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


def _measure(document, transcript, reference=None, annotations=None):
    tracemalloc.start()
    begin = time.perf_counter()
    try:
        result = align(document, transcript)
        elapsed = time.perf_counter() - begin
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Score the actual timestamp precision the output file would have, rather
    # than the model's unrounded floats. Abstention leaves the input unchanged.
    output = (parse_subtitle(document.render(result.timings), document.format)
              if result.timings is not None else document)
    timings = [(c.start, c.end) for c in output.cues]
    row = {'candidate': 'text-anchor-global-affine', 'status': result.status, 'reason': result.reason,
           'runtime_seconds': elapsed, 'peak_python_allocated_bytes': peak,
           'peak_process_rss_bytes': None, 'report': result.report,
           'source_modified': False, 'proposal_available': result.timings is not None,
           'effective_output': 'candidate' if result.timings is not None else 'unchanged_input_on_abstention',
           'preserved_non_timing_content': preserved_content(document, output)}
    if reference is not None:
        row['before'] = timing_metrics([(c.start, c.end) for c in document.cues], reference)
        row['after'] = timing_metrics(timings, reference)
        row['harmful_changes'] = sum(abs(c.start - truth[0]) < .1 and abs(t[0] - truth[0]) > .5
                                    for c, t, truth in zip(document.cues, timings, reference))
    if annotations is not None:
        row['reviewed_timing'] = compare_reviewed(document, output, annotations)
    return row


def _write_output(path, serialized, protected_paths):
    """Exclusive creation also refuses aliases/hardlinks to existing inputs."""
    destination = Path(path)
    if destination.resolve() in {Path(p).resolve() for p in protected_paths if p}:
        raise ValueError('Output must not replace a benchmark input')
    with destination.open('x', encoding='utf-8') as output:
        output.write(serialized + '\n')


def _baseline(document, args, reference, annotations):
    with tempfile.TemporaryDirectory(prefix='bazarr-benchmark-') as temporary:
        # Baseline tools receive a scratch subtitle too. They cannot mutate the
        # selected source even if their input-writing behavior changes.
        source = Path(temporary) / ('input.' + document.format)
        source.write_bytes(document.raw)
        output = Path(temporary) / ('baseline.' + document.format)
        command = [args.ffsubsync, args.media, '-i', str(source), '-o', str(output)]
        options = list(args.ffsubsync_arg or [])
        if args.audio_index is not None:
            options.append('--reference-stream=0:' + str(args.audio_index))
            if not any(option.startswith('--vad=') for option in options):
                options.append('--vad=webrtc')
        command.extend(options)
        begin = time.perf_counter()
        completed = subprocess.run(command, capture_output=True, timeout=21600)
        row = {'candidate': 'ffsubsync', 'exit_code': completed.returncode,
               'arguments': options,
               'runtime_seconds': time.perf_counter() - begin,
               'peak_process_rss_bytes': None, 'source_modified': False}
        if completed.returncode == 0 and output.is_file():
            baseline = load_subtitle(output)
            row['preserved_cue_text'] = [c.text for c in baseline.cues] == [c.text for c in document.cues]
            row['preserved_non_timing_content'] = preserved_content(document, baseline)
            if reference is not None:
                row['after'] = (timing_metrics([(c.start, c.end) for c in baseline.cues], reference)
                                if row['preserved_non_timing_content'] else
                                {'comparable': False, 'reason': 'Output changed non-timing content'})
            if annotations is not None:
                row['reviewed_timing'] = compare_reviewed(document, baseline, annotations)
        else:
            row['stderr_tail'] = completed.stderr.decode(errors='replace')[-2000:]
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
    parser.add_argument('--timing-reference', help='Reviewed sparse JSON reference; never used for model fitting')
    parser.add_argument('--make-reference-template', action='store_true')
    parser.add_argument('--sample-count', type=int, default=12, help='Review-template cue count (1–1000)')
    parser.add_argument('--audio-index', type=int, help='Selected media audio stream index for human review')
    parser.add_argument('--media')
    parser.add_argument('--ffsubsync', help='Baseline executable (writes only a temporary output)')
    parser.add_argument('--ffsubsync-arg', action='append', help='Repeat as --ffsubsync-arg=--option; record installed profile settings')
    parser.add_argument('--output', help='New private report/template file; existing files are never overwritten')
    args = parser.parse_args(argv)
    if args.audio_index is not None and args.audio_index < 0:
        parser.error('--audio-index must be nonnegative')
    if args.synthetic and any((args.subtitle, args.transcript, args.reference, args.timing_reference,
                               args.make_reference_template, args.media, args.ffsubsync,
                               args.ffsubsync_arg, args.audio_index is not None)):
        parser.error('--synthetic cannot be combined with real-media options')
    if args.reference and args.timing_reference:
        parser.error('Choose either --reference or --timing-reference')
    if args.make_reference_template and any((args.transcript, args.reference, args.timing_reference, args.ffsubsync)):
        parser.error('Template creation cannot be combined with scoring options')
    if (args.make_reference_template or args.timing_reference) and (not args.media or args.audio_index is None):
        parser.error('--media and --audio-index are required for timing references')
    if args.ffsubsync_arg and not args.ffsubsync:
        parser.error('--ffsubsync-arg requires --ffsubsync')
    if args.ffsubsync and not args.media:
        parser.error('--media is required for ffsubsync baseline')
    # Baseline flags are options only: deny filenames/positional references or
    # switches that can redirect its input/output away from our scratch files.
    for option in args.ffsubsync_arg or []:
        if (not option.startswith('--') or option.split('=', 1)[0] not in
                {'--vad', '--no-fix-framerate', '--gss', '--max-offset-seconds',
                 '--skip-infer-framerate-ratio'}):
            parser.error('Unsupported baseline option; use --option=value for valued flags')
        name, separator, value = option.partition('=')
        valued = name in {'--vad', '--max-offset-seconds'}
        if valued != bool(separator) or (valued and not value):
            parser.error('Baseline valued flags require =value; boolean flags take no value')
    if args.synthetic:
        report = synthetic_benchmark()
    else:
        if not args.subtitle or (not args.make_reference_template and not args.transcript):
            parser.error('--subtitle and --transcript are required without --synthetic')
        document = load_subtitle(args.subtitle)
        if args.make_reference_template:
            report = reference_template(document, media_fingerprint(args.media), args.audio_index, args.sample_count)
            serialized = json.dumps(report, indent=2, allow_nan=False)
            if args.output:
                _write_output(args.output, serialized, [args.subtitle, args.media])
            else:
                print(serialized)
            return 0
        annotations = None
        reference_summary = None
        media_identity = None
        if args.timing_reference:
            reference_bytes = Path(args.timing_reference).read_bytes()
            reference_data = json.loads(reference_bytes)
            media_identity = media_fingerprint(args.media)
            annotations = validate_reference(document, reference_data, media_identity, args.audio_index)
            reference_summary = dict(reference_coverage(document, annotations),
                                     independently_reviewed=True, verification='user-attestation-only',
                                     used_for_model_fitting=False, audio_index=args.audio_index,
                                     artifact_sha256=hashlib.sha256(reference_bytes).hexdigest(),
                                     transcript_provenance='caller-supplied; verify media, stream and time origin')
        transcript_bytes = Path(args.transcript).read_bytes()
        transcript = json.loads(transcript_bytes)
        transcript_identity = None
        if isinstance(transcript, dict):
            transcript_identity = transcript.get('identity')
            transcript = transcript['transcript']
        if media_identity is not None and transcript_identity is not None:
            if (not isinstance(transcript_identity, dict) or
                    transcript_identity.get('media_sha256') != media_identity['sha256'] or
                    type(transcript_identity.get('audio_index')) is not int or
                    transcript_identity['audio_index'] != args.audio_index):
                raise ValueError('Transcript identity does not match the reviewed media/audio stream')
            reference_summary['transcript_provenance'] = 'caller-supplied identity matches media/audio; time origin remains caller responsibility'
        reference_document = load_subtitle(args.reference) if args.reference else None
        reference = [(c.start, c.end) for c in reference_document.cues] if reference_document else None
        if reference_document and [c.text for c in document.cues] != [c.text for c in reference_document.cues]:
            parser.error('Reference text/order differs; provide an independently mapped cue reference')
        report = {'schema': 2, 'synthetic': False, 'quality_claim': False,
                  'reference_independently_verified': False,
                  'timing_reference': reference_summary,
                  'inputs': {'subtitle_sha256': document.sha256, 'subtitle_format': document.format,
                             'transcript_sha256': hashlib.sha256(transcript_bytes).hexdigest(),
                             'transcript_identity': transcript_identity,
                             'media': media_identity, 'audio_index': args.audio_index},
                  'results': [_measure(document, transcript, reference, annotations)]}
        if args.ffsubsync:
            report['results'].append(_baseline(document, args, reference, annotations))
    serialized = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        _write_output(args.output, serialized, [args.subtitle, args.media, args.transcript,
                                               args.reference, args.timing_reference])
    else:
        print(serialized)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Private, scratch-only human timing references and correspondence-safe scoring."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import statistics

from .subtitles import SubtitleDocument


def media_fingerprint(path):
    """Hash bytes, rather than bind an annotation to a mutable filename."""
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    after = path.stat()
    if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
            (after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size):
        raise ValueError('Media changed while computing its timing-reference identity')
    return {'sha256': digest.hexdigest(), 'size_bytes': size}


def reference_template(document, media_identity, audio_index, sample_count=12):
    if type(audio_index) is not int or audio_index < 0:
        raise ValueError('Specify a nonnegative audio stream index for human review')
    if type(sample_count) is not int or not 1 <= sample_count <= 1000:
        raise ValueError('Sample count must be between 1 and 1000')
    cues = sorted(document.cues, key=lambda c: (c.start, c.index))
    count = min(sample_count, len(cues))
    # Spread review points over time, including both boundaries. A long silence
    # may map multiple targets to one cue, so choose the nearest unused cue.
    chosen = []
    available = list(cues)
    for i in range(count):
        target = cues[0].start + (cues[-1].start - cues[0].start) * i / max(1, count - 1)
        cue = min(available, key=lambda c: (abs(c.start - target), c.index))
        chosen.append(cue)
        available.remove(cue)
    return {
        'schema': 1, 'kind': 'bazarr-sync-timing-reference',
        'subtitle_sha256': document.sha256, 'format': document.format,
        'cue_count': len(document.cues), 'media': media_identity,
        'audio_index': audio_index, 'independently_reviewed': False,
        'instructions': 'Listen to the selected audio in your own player. Enter intended cue start/end seconds; leave unknown endpoints null. Set independently_reviewed to true only after review. Source timings are context, not truth. Keep this file private.',
        'annotations': [
            {'cue_index': cue.index, 'text': cue.text,
             'source_start_seconds': cue.start, 'source_end_seconds': cue.end,
             'start_seconds': None, 'end_seconds': None}
            for cue in sorted(chosen, key=lambda c: c.index)
        ],
    }


def validate_reference(document, reference, media_identity, audio_index):
    if not isinstance(reference, dict) or type(reference.get('schema')) is not int or reference['schema'] != 1 or reference.get('kind') != 'bazarr-sync-timing-reference':
        raise ValueError('Unsupported timing reference schema')
    if (reference.get('subtitle_sha256') != document.sha256 or
            reference.get('format') != document.format or
            type(reference.get('cue_count')) is not int or
            reference['cue_count'] != len(document.cues)):
        raise ValueError('Timing reference belongs to a different subtitle version')
    if reference.get('media') != media_identity:
        raise ValueError('Timing reference belongs to different media bytes')
    track = reference.get('audio_index')
    if type(track) is not int or track < 0 or type(audio_index) is not int or track != audio_index:
        raise ValueError('Timing reference audio stream does not match --audio-index')
    if reference.get('independently_reviewed') is not True:
        raise ValueError('Timing reference must explicitly attest independent human review')
    rows = reference.get('annotations')
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(document.cues):
        raise ValueError('Timing reference requires a bounded annotation list')
    seen, annotations = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Timing annotations must be objects')
        index = row.get('cue_index')
        if type(index) is not int or index < 0 or index >= len(document.cues) or index in seen:
            raise ValueError('Annotation cue indices must be unique and in range')
        seen.add(index)
        cue = document.cues[index]
        if (row.get('text') != cue.text or row.get('source_start_seconds') != cue.start or
                row.get('source_end_seconds') != cue.end):
            raise ValueError('Annotation cue context differs from the source subtitle')
        values = [row.get('start_seconds'), row.get('end_seconds')]
        for value in values:
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError('Reviewed timestamps must be finite nonnegative seconds or null')
        start, end = values
        if start is not None and end is not None and end <= start:
            raise ValueError('Reviewed cue end must follow its start')
        if start is not None or end is not None:
            annotations.append({'cue_index': index, 'start_seconds': start, 'end_seconds': end})
    if not annotations:
        raise ValueError('Timing reference contains no reviewed timestamps')
    return annotations


def reference_coverage(document, annotations):
    first = min(c.start for c in document.cues)
    span = max(c.start for c in document.cues) - first
    positions = [document.cues[a['cue_index']].start for a in annotations]
    thirds = dict(early=0, middle=0, late=0)
    for position in positions:
        region = min(2, int(3 * (position - first) / span)) if span else 0
        thirds[('early', 'middle', 'late')[region]] += 1
    return {
        'annotated_cue_count': len(annotations), 'total_cue_count': len(document.cues),
        'annotated_cue_fraction': len(annotations) / len(document.cues),
        'source_time_span_fraction': (max(positions) - min(positions)) / span if span else 0.,
        'source_time_thirds': thirds,
        'unreviewed_cues_are_unknown': True, 'release_gate_established': False,
    }


def _summary(errors):
    if not errors:
        return {'count': 0}
    values = sorted(errors)
    return {'count': len(values), 'mean_absolute_error_seconds': statistics.mean(values),
            'median_absolute_error_seconds': statistics.median(values),
            'p95_absolute_error_seconds': values[math.ceil(.95 * len(values)) - 1],
            'maximum_absolute_error_seconds': values[-1]}


def reviewed_metrics(timings, annotations):
    starts, ends = [], []
    for annotation in annotations:
        start, end = timings[annotation['cue_index']]
        if annotation['start_seconds'] is not None:
            starts.append(abs(start - annotation['start_seconds']))
        if annotation['end_seconds'] is not None:
            ends.append(abs(end - annotation['end_seconds']))
    return {'comparable': True, 'starts': _summary(starts), 'ends': _summary(ends),
            'all_reviewed_endpoints': _summary(starts + ends)}


def preserved_content(source: SubtitleDocument, output: SubtitleDocument):
    """Compare everything except timestamp spans, including style/encoding/BOM."""
    def content(document):
        spans = sorted(span for pair in document.spans for span in pair)
        parts, position = [], 0
        for start, end in spans:
            parts.append(document.source[position:start])
            position = end
        parts.append(document.source[position:])
        return document.format, document.encoding, document.bom, parts
    return len(source.cues) == len(output.cues) and content(source) == content(output)


def compare_reviewed(document, output, annotations):
    """Never score by cue position after text/order/format preservation fails."""
    if not preserved_content(document, output):
        return {'comparable': False, 'reason': 'Output changed non-timing content; cue correspondence is not safe'}
    before = [(c.start, c.end) for c in document.cues]
    after = [(c.start, c.end) for c in output.cues]
    harmful = set()
    for annotation in annotations:
        index = annotation['cue_index']
        for endpoint, name in enumerate(('start_seconds', 'end_seconds')):
            truth = annotation[name]
            if truth is not None and abs(before[index][endpoint] - truth) <= .1 and abs(after[index][endpoint] - truth) > .5:
                harmful.add(index)
    return {'comparable': True, 'before': reviewed_metrics(before, annotations),
            'after': reviewed_metrics(after, annotations),
            'diagnostic_harmful_cue_count': len(harmful),
            'diagnostic_thresholds_seconds': {'near_correct_at_most': .1, 'harmful_error_above': .5},
            'thresholds_are_release_criteria': False}

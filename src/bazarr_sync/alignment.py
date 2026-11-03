"""Experimental text-anchor alignment. Thresholds are engineering guards, not probabilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import math
import re
import statistics
from .subtitles import SubtitleDocument


@dataclass
class AlignmentResult:
    status: str
    reason: str
    timings: list[tuple[float, float]] | None = None
    report: dict = field(default_factory=dict)


def normalized(text: str) -> str:
    text = re.sub(r'\{[^}]*\}|<[^>]*>|\[[^]]*\]', ' ', text)
    text = text.replace('\\N', ' ').replace('\\n', ' ')
    return ' '.join(re.findall(r"[a-z0-9]+", text.lower()))


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    xmean = statistics.mean(p[0] for p in points)
    ymean = statistics.mean(p[1] for p in points)
    denominator = sum((x - xmean) ** 2 for x, _ in points)
    if denominator == 0:
        return 1., statistics.median(y - x for x, y in points)
    slope = sum((x - xmean) * (y - ymean) for x, y in points) / denominator
    return slope, ymean - slope * xmean


def _check_timings(document, timings, media_duration):
    if any(start < 0 or end <= start or not all(math.isfinite(v) for v in (start, end)) for start, end in timings):
        raise ValueError('Correction would create invalid cue times')
    if media_duration is not None and max(end for _, end in timings) > media_duration + .05:
        raise ValueError('Correction exceeds media duration')
    # Preserve intentional overlaps; prohibit newly reversed event order.
    for first, second, before, after in zip(document.cues, document.cues[1:], timings, timings[1:]):
        if first.start <= second.start and before[0] > after[0]:
            raise ValueError('Correction reverses cue order')
    document.render(timings)  # Includes format-specific preservation guards.


def align(document: SubtitleDocument, transcript: list[dict], media_duration: float | None = None) -> AlignmentResult:
    """Return a proposal only; this function never writes source files.

    Matching is deliberately conservative. Distinctive >=4-word cues match
    variable-length transcript word windows, with uniqueness and monotonicity guards. Alternating
    anchors are held out from fitting. Cuts/section jumps abstain in this version.
    """
    report = {'experimental': True, 'automatic_release_validated': False, 'model': 'global-affine', 'thresholds': {'minimum_anchors': 8, 'maximum_holdout_error_seconds': .65}}
    if not isinstance(transcript, list) or len(transcript) > 100000:
        raise ValueError('Transcript must be a bounded list')
    prepared = []
    previous = -1.
    for segment in transcript:
        start, end = float(segment['start']), float(segment['end'])
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start or start < previous:
            raise ValueError('Transcript intervals must be finite, valid and ordered')
        previous = start
        prepared.append((normalized(str(segment['text'])), start, end))
    tokens = []
    for text, start, end in prepared:
        words = text.split()
        for position, word in enumerate(words):
            tokens.append((word, start + (end - start) * position / len(words),
                           start + (end - start) * (position + 1) / len(words)))
    positions = {}
    for index, token in enumerate(tokens):
        positions.setdefault(token[0], []).append(index)
    anchors = []
    for cue in document.cues:
        text = normalized(cue.text)
        if len(text.split()) < 4:
            continue
        # Use token-index candidates rather than compare every cue with every segment.
        cue_words = text.split()
        words = set(cue_words)
        candidates = set()
        for offset, word in enumerate(cue_words[:3]):
            for position in positions.get(word, []):
                for deviation in (-1, 0, 1):
                    start = position - offset + deviation
                    if start >= 0:
                        candidates.add(start)
        ranked = []
        windows = []
        for index in sorted(candidates):
            for width in range(max(3, len(cue_words) - 2), len(cue_words) + 3):
                group = tokens[index:index + width]
                if len(group) != width or any(b[1] - a[2] > 3. for a, b in zip(group, group[1:])):
                    continue
                windows.append((' '.join(p[0] for p in group), group[0][1], group[-1][2], index, width))
        for window in windows:
            wtext = window[0]
            other = set(wtext.split())
            overlap = len(words & other) / max(1, len(words | other))
            if overlap < .55:
                continue
            score = SequenceMatcher(None, text, wtext, autojunk=False).ratio()
            if score >= .82:
                ranked.append((score, window))
        ranked.sort(key=lambda r: r[0], reverse=True)
        if not ranked:
            continue
        score, best = ranked[0]
        # Near-identical spans around the same dialogue are one candidate.
        rivals = [s for s, w in ranked[1:] if abs(w[1] - best[1]) > 2.]
        if rivals and score - max(rivals) < .08:
            continue
        anchors.append({'cue_index': cue.index, 'source': cue.start, 'target': best[1], 'similarity': score})
    report['matched_anchors'] = anchors
    report['coverage'] = len(anchors) / len(document.cues)
    if len(anchors) < 8:
        return AlignmentResult('uncertain', 'Insufficient distinctive transcript matches', report=report)
    if any(a['target'] >= b['target'] for a, b in zip(anchors, anchors[1:])):
        return AlignmentResult('uncertain', 'Transcript matches are not monotonic', report=report)
    span = document.cues[-1].start - document.cues[0].start
    if span <= 0 or anchors[0]['cue_index'] > len(document.cues) * .15 or anchors[-1]['cue_index'] < len(document.cues) * .85:
        return AlignmentResult('uncertain', 'Evidence does not cover subtitle boundaries', report=report)
    if report['coverage'] < .60:
        return AlignmentResult('uncertain', 'Matched evidence covers too few cues', report=report)
    train = anchors[::2]
    holdout = anchors[1::2]
    slope, intercept = _fit([(a['source'], a['target']) for a in train])
    residuals = [abs(slope * a['source'] + intercept - a['target']) for a in holdout]
    report.update(slope=slope, offset_seconds=intercept, holdout_count=len(holdout), holdout_max_error_seconds=max(residuals), holdout_median_error_seconds=statistics.median(residuals))
    if not .90 <= slope <= 1.10 or max(residuals) > .65:
        return AlignmentResult('uncertain', 'Offset/drift model fails independent timing checks; possible cuts or wrong matches', report=report)
    timings = [(slope * cue.start + intercept, slope * cue.end + intercept) for cue in document.cues]
    try:
        _check_timings(document, timings, media_duration)
    except ValueError as error:
        return AlignmentResult('uncertain', str(error), report=report)
    maximum_change = max(abs(start - cue.start) for cue, (start, _) in zip(document.cues, timings))
    report['maximum_change_seconds'] = maximum_change
    if maximum_change < .10:
        return AlignmentResult('unchanged', 'No material correction supported', [(c.start, c.end) for c in document.cues], report)
    return AlignmentResult('preview', 'Experimental correction passes internal checks; release quality has not been established', timings, report)


def manual_preview(document: SubtitleDocument, anchors: list[dict], media_duration: float | None = None) -> AlignmentResult:
    """Piecewise-linear cue-start correction; one anchor applies a constant shift.

    Outside multi-anchor intervals, keep the nearest anchor's offset. Durations
    follow the local slope within intervals. All results require explicit Apply.
    """
    if not anchors:
        raise ValueError('At least one manual anchor is required')
    points = []
    last_index = -1
    for anchor in anchors:
        index, target = anchor['cue_index'], float(anchor['time'])
        if isinstance(index, bool) or not isinstance(index, int) or index <= last_index or index >= len(document.cues):
            raise ValueError('Cue indices must be unique, increasing and in range')
        if not math.isfinite(target) or target < 0 or (media_duration is not None and target >= media_duration):
            raise ValueError('Anchor target is outside media bounds')
        source = document.cues[index].start
        if points and (source <= points[-1][0] or target <= points[-1][1]):
            raise ValueError('Anchors must have increasing source and target times')
        points.append((source, target))
        last_index = index
    def mapping(value):
        if value <= points[0][0]:
            return value + points[0][1] - points[0][0]
        for (sx, tx), (sy, ty) in zip(points, points[1:]):
            if value <= sy:
                return tx + (value - sx) * (ty - tx) / (sy - sx)
        return value + points[-1][1] - points[-1][0]
    timings = [(mapping(c.start), mapping(c.end)) for c in document.cues]
    _check_timings(document, timings, media_duration)
    return AlignmentResult('preview', 'Manual cue-start anchors; explicit Apply required', timings,
                           {'manual': True, 'anchors': anchors, 'extrapolation': 'nearest-anchor-offset', 'requires_apply': True})

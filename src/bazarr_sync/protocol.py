"""Versioned coordinator/worker contract: result validation and the automatic-apply confidence policy.

The worker returns per-cue timings only. The coordinator re-derives every
check below from the source document; nothing a worker reports about its own
confidence can authorize publication.
"""
from __future__ import annotations

import math
import re

PROTOCOL_VERSION = 1
CUE_STATUSES = {'aligned', 'legacy_inferred', 'unresolved'}
RESULT_ID = re.compile(r'[A-Za-z0-9_-]{8,64}')


def validate_result(body: dict, *, job_id: str, subtitle_sha256: str, audio_sha256: str,
                    cue_count: int, duration_seconds: float) -> dict:
    """Strict schema/identity/numeric checks. Returns the accepted result fields or raises ValueError."""
    if body.get('protocol') != PROTOCOL_VERSION:
        raise ValueError('Unsupported worker protocol version')
    if body.get('job_id') != job_id:
        raise ValueError('Result belongs to a different job')
    if not isinstance(body.get('result_id'), str) or not RESULT_ID.fullmatch(body['result_id']):
        raise ValueError('result_id must be 8-64 characters of [A-Za-z0-9_-]')
    if body.get('subtitle_sha256') != subtitle_sha256 or body.get('audio_sha256') != audio_sha256:
        raise ValueError('Result was computed from different subtitle or audio input')
    timings = body.get('timings')
    if not isinstance(timings, list) or len(timings) != cue_count:
        raise ValueError('Result must contain exactly one timing per source cue')
    accepted = []
    for index, row in enumerate(timings):
        if not isinstance(row, dict) or row.get('cue_index') != index:
            raise ValueError('Timings must be ordered by zero-based cue index')
        start, end, status = row.get('start'), row.get('end'), row.get('status')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)):
            raise ValueError(f'Cue {index} has a non-numeric time')
        if not 0 <= start < end <= duration_seconds + 1:
            raise ValueError(f'Cue {index} is outside media bounds or not increasing')
        if status not in CUE_STATUSES:
            raise ValueError(f'Cue {index} has an unknown status')
        accepted.append({'cue_index': index, 'start': float(start), 'end': float(end), 'status': status})
    engine = body.get('engine')
    diagnostics = body.get('diagnostics')
    if not isinstance(engine, dict) or not isinstance(diagnostics, dict):
        raise ValueError('engine and diagnostics objects are required')
    return {'result_id': body['result_id'], 'timings': accepted, 'engine': engine, 'diagnostics': diagnostics}


def geometry(source_cues, timings) -> dict:
    """Problems the proposal introduces relative to the source document."""
    pairs = list(zip(source_cues, timings))
    return {
        'start_inversions': [b['cue_index'] for (_, a), (_, b) in zip(pairs, pairs[1:]) if b['start'] < a['start'] - .001],
        'new_overlaps': [b['cue_index'] for (x, a), (y, b) in zip(pairs, pairs[1:])
                         if max(0, a['end'] - b['start']) > max(0, x.end - y.start) + .05],
    }


def evaluate_confidence(source_cues, timings, *, min_direct_coverage: float) -> dict:
    """Owner-agreed policy (2026-11-03): apply automatically only when most spoken cues were measured
    directly and the proposal introduces no ordering/overlap problems. Everything else waits for review.
    Measured basis: S1E2/S1E4 held-out runs reached ~88-90% direct coverage with zero geometry problems."""
    from .timing import tokens
    spoken = [i for i, cue in enumerate(source_cues) if tokens(cue.text)]
    direct = sum(timings[i]['status'] == 'aligned' for i in spoken)
    coverage = direct / len(spoken) if spoken else 0.0
    shape = geometry(source_cues, timings)
    reasons = []
    if coverage < min_direct_coverage:
        reasons.append(f'direct coverage {coverage:.0%} is below {min_direct_coverage:.0%}')
    if shape['start_inversions']:
        reasons.append(f"{len(shape['start_inversions'])} cue(s) would start before the previous cue")
    if shape['new_overlaps']:
        reasons.append(f"{len(shape['new_overlaps'])} new overlap(s) between cues")
    return {'eligible': not reasons, 'reasons': reasons, 'direct_coverage': round(coverage, 4),
            'spoken_cues': len(spoken), 'direct_cues': direct,
            'start_inversions': len(shape['start_inversions']), 'new_overlaps': len(shape['new_overlaps']),
            'min_direct_coverage': min_direct_coverage}

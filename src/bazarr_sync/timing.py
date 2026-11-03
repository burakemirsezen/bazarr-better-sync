"""Cue timing from speech evidence: tolerant sequence mapping, section gate and conflict resolution.

This is the sequence mapper v0.2 that was frozen and evaluated on 2026-11-03
(see docs/benchmark-sequence-mapper-2026-11-03.md). Cue wording is never edited;
every function only proposes times. rapidfuzz performs the global token
alignment; ALASS (run by the worker) supplies the section prior used by gate().
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, asdict
import re
import statistics
import unicodedata


@dataclass(frozen=True)
class SeqConfig:
    band_seconds: float = 15.0          # drop matched pairs further apart than this
    gap_lookahead: int = 3              # ASR tokens searched for a fuzzy partner inside a gap
    fuzzy_min_ratio: float = 0.75
    fuzzy_min_length: int = 4
    min_coverage: float = 0.6
    min_matched_tokens: int = 2
    short_cue_tokens: int = 2           # cues this short need unique context
    short_context_max_seconds: float = 30.0
    max_change_seconds: float = 10.0
    max_smoothing_seconds: float = 0.5
    caption_lead_seconds: float = 0.1
    caption_trail_seconds: float = 0.2
    min_display_seconds: float = 0.5
    # Gate: ALASS section map is a prior, checked against nearby direct anchors
    gate_neighbour_cues: int = 5        # anchors within this many cue indices either side
    gate_min_anchors: int = 3
    gate_max_spread_seconds: float = 0.25   # median absolute deviation of neighbour shifts
    gate_override_seconds: float = 0.5      # prior replaced when consistent anchors disagree by more
    gate_direct_tolerance_seconds: float = 0.75


def config_manifest(cfg: SeqConfig) -> dict:
    return {'mapper': 'sequence', 'version': '0.2', **asdict(cfg)}


TAG = re.compile(r'<[^>]*>|\{[^}]*\}')
SFX = re.compile(r'\[[^\]]*\]|♪[^♪]*♪?')


def tokens(text: str) -> list[str]:
    t = SFX.sub(' ', TAG.sub(' ', text))
    t = ''.join(c for c in unicodedata.normalize('NFKD', t) if not unicodedata.combining(c)).lower()
    return re.findall(r'[^\W_]+', re.sub(r"[’'`]", '', t))


def is_fuzzy(a, b, cfg):
    from rapidfuzz.distance import Levenshtein
    return min(len(a), len(b)) >= cfg.fuzzy_min_length and Levenshtein.normalized_similarity(a, b) >= cfg.fuzzy_min_ratio


def _gap_pairs(sub, asr, s0, s1, d0, d1, cfg):
    """Monotone pairing inside one non-equal region: compound merges first, then fuzzy words."""
    pairs = []
    i, j = s0, d0
    while i < s1 and j < d1:
        if i + 1 < s1 and sub[i] + sub[i + 1] == asr[j]:
            pairs.append(([i, i + 1], [j], 'merge')); i += 2; j += 1; continue
        if j + 1 < d1 and sub[i] == asr[j] + asr[j + 1]:
            pairs.append(([i], [j, j + 1], 'merge')); i += 1; j += 2; continue
        hit = next((k for k in range(j, min(d1, j + cfg.gap_lookahead)) if is_fuzzy(sub[i], asr[k], cfg)), None)
        if hit is None:
            i += 1; continue
        pairs.append(([i], [hit], 'fuzzy')); i += 1; j = hit + 1
    return pairs


def align(sub, sub_times, asr, asr_times, cfg):
    """Return [(sub_indices, asr_indices, kind)] matched along rapidfuzz's global token alignment.
    Pairs whose times disagree by more than band_seconds are dropped."""
    from rapidfuzz.distance import Levenshtein
    pairs = []
    ops = list(Levenshtein.opcodes(sub, asr))
    k = 0
    while k < len(ops):
        op = ops[k]
        if op.tag == 'equal':
            pairs += [([op.src_start + x], [op.dest_start + x], 'exact') for x in range(op.src_end - op.src_start)]
            k += 1
            continue
        s0, d0 = op.src_start, op.dest_start
        while k < len(ops) and ops[k].tag != 'equal':
            k += 1
        pairs += _gap_pairs(sub, asr, s0, ops[k - 1].src_end, d0, ops[k - 1].dest_end, cfg)
    return [p for p in pairs if all(abs(asr_times[p[1][0]] - sub_times[s]) <= cfg.band_seconds for s in p[0])]


def _occurrences(pattern, asr, begin, end):
    k = len(pattern)
    return sum(asr[x:x + k] == pattern for x in range(max(0, begin), min(len(asr), end) - k + 1))


def map_cues(cues, words, cfg=SeqConfig(), media_duration_seconds=None):
    """cues: cue_index/text/start_seconds/end_seconds; words: text/start_time/end_time/smoothing_seconds
    (+optional timing_supported), all on one absolute timeline. Returns one row per cue, in order."""
    words = sorted(words, key=lambda w: (w['start_time'], w['end_time']))
    asr, asr_word, asr_first, asr_last, asr_times = [], [], [], [], []
    for wi, w in enumerate(words):
        ts = tokens(w['text'])
        for p, t in enumerate(ts):
            asr.append(t); asr_word.append(wi); asr_first.append(p == 0); asr_last.append(p == len(ts) - 1)
            asr_times.append(w['start_time'])
    sub, sub_cue, sub_pos, sub_times, counts = [], [], [], [], []
    for ci, c in enumerate(cues):
        ts = tokens(c['text'])
        counts.append(len(ts))
        span = c['end_seconds'] - c['start_seconds']
        for p, t in enumerate(ts):
            sub.append(t); sub_cue.append(ci); sub_pos.append(p)
            sub_times.append(c['start_seconds'] + span * (p + .5) / len(ts))
    pairs = align(sub, sub_times, asr, asr_times, cfg) if sub and asr else []
    matched = [[] for _ in cues]
    for sis, ais, kind in pairs:
        for si in sis:
            matched[sub_cue[si]].append((sub_pos[si], ais, kind, sub_times[si]))

    def timed(w):
        return (w.get('timing_supported', True) and w['end_time'] > w['start_time']
                and w.get('smoothing_seconds', 0) <= cfg.max_smoothing_seconds)

    def unresolved(c, reason, **extra):
        return dict(cue_index=c['cue_index'], start_seconds=c['start_seconds'], end_seconds=c['end_seconds'],
                    status='unresolved', reason=reason, **extra)

    claimed = {a for m in matched for _, ais, _, _ in m for a in ais}

    def duplicated(c, m):
        """An identical copy of the matched ASR span nearby that no cue claims makes the choice ambiguous."""
        b = min(a for _, ais, _, _ in m for a in ais)
        e = max(a for _, ais, _, _ in m for a in ais) + 1
        pattern = asr[b:e]
        lo = bisect.bisect_left(asr_times, c['start_seconds'] - cfg.band_seconds)
        hi = bisect.bisect_right(asr_times, c['end_seconds'] + cfg.band_seconds)
        return any(x != b and asr[x:x + e - b] == pattern and not claimed.intersection(range(x, x + e - b))
                   for x in range(lo, max(lo, hi - (e - b) + 1)))

    # Pass 1: cues long enough to stand on their own.
    accepted, ambiguous = {}, set()
    for ci, (c, m, n) in enumerate(zip(cues, matched, counts)):
        if n == 0:
            continue
        cov = len({p for p, *_ in m}) / n
        if n > cfg.short_cue_tokens and len({p for p, *_ in m}) >= cfg.min_matched_tokens and cov >= cfg.min_coverage:
            if duplicated(c, m):
                ambiguous.add(ci)
            else:
                accepted[ci] = True
    keys = sorted(accepted)
    rows = []
    for ci, (c, m, n) in enumerate(zip(cues, matched, counts)):
        positions = {p for p, *_ in m}
        cov = len(positions) / n if n else 0
        diag = dict(source_token_count=n, matched_token_count=len(positions), coverage=round(cov, 3),
                    match_kinds=sorted({k for *_, k, _ in m}),
                    asr_token_begin=min((a for _, ais, _, _ in m for a in ais), default=None),
                    asr_token_end=max((a + 1 for _, ais, _, _ in m for a in ais), default=None))
        if n == 0:
            rows.append(unresolved(c, 'no_spoken_text', **diag)); continue
        if not m:
            rows.append(unresolved(c, 'no_matched_tokens', **diag)); continue
        if n <= cfg.short_cue_tokens:
            if len(positions) < n or any(k == 'fuzzy' for *_, k, _ in m):
                rows.append(unresolved(c, 'short_cue_partial_match', **diag)); continue
            x = bisect.bisect_left(keys, ci)
            prev = keys[x - 1] if x else None
            nxt = keys[x] if x < len(keys) else None
            if x < len(keys) and keys[x] == ci:
                nxt = keys[x + 1] if x + 1 < len(keys) else None
            if prev is None or nxt is None:
                rows.append(unresolved(c, 'short_cue_without_context', **diag)); continue
            pb = max(a for _, ais, _, _ in matched[prev] for a in ais) + 1
            nb = min(a for _, ais, _, _ in matched[nxt] for a in ais)
            if asr_times[min(nb, len(asr) - 1)] - asr_times[min(pb, len(asr) - 1)] > cfg.short_context_max_seconds:
                rows.append(unresolved(c, 'short_cue_context_too_wide', **diag)); continue
            pattern = [asr[a] for _, ais, _, _ in sorted(m) for a in ais]
            if _occurrences(pattern, asr, pb, nb) != 1:
                rows.append(unresolved(c, 'repeated_phrase_ambiguous_in_context', **diag)); continue
        elif ci in ambiguous:
            rows.append(unresolved(c, 'repeated_phrase_ambiguous', **diag)); continue
        elif ci not in accepted:
            rows.append(unresolved(c, 'low_coverage', **diag)); continue
        m = sorted(m)
        first, last = m[0], m[-1]
        start = end = None
        a0, a1 = first[1][0], last[1][-1]
        w0, w1 = words[asr_word[a0]], words[asr_word[a1]]
        if first[0] == 0 and asr_first[a0] and timed(w0):
            start = w0['start_time'] - cfg.caption_lead_seconds
        if last[0] == n - 1 and asr_last[a1] and timed(w1):
            end = w1['end_time'] + cfg.caption_trail_seconds
        shift = statistics.median(asr_times[ais[0]] - t for _, ais, _, t in m)
        if start is None and end is None:
            start, end, method = c['start_seconds'] + shift, c['end_seconds'] + shift, 'shift_median'
        elif start is None:
            start, method = end - (c['end_seconds'] - c['start_seconds']), 'end_direct'
        elif end is None:
            end, method = start + (c['end_seconds'] - c['start_seconds']), 'start_direct'
        else:
            method = 'both_direct'
        start = max(0.0, start)
        end = max(end, start + cfg.min_display_seconds)
        if media_duration_seconds is not None:
            end = min(end, media_duration_seconds)
        if max(abs(start - c['start_seconds']), abs(end - c['end_seconds'])) > cfg.max_change_seconds:
            rows.append(unresolved(c, 'large_change_requires_review', **diag)); continue
        if end <= start:
            rows.append(unresolved(c, 'invalid_proposal', **diag)); continue
        rows.append(dict(cue_index=c['cue_index'], start_seconds=start, end_seconds=end, status='aligned',
                         reason=None, method=method, shift_estimate=shift, **diag))
    return rows


def gate(cues, rows, prior_starts, cfg=SeqConfig()):
    """Combine direct rows with a section prior (ALASS output starts, one per cue).
    Returns (direct rows to keep, per-cue prior shift, decisions). Direct times survive only when
    they agree with the (possibly corrected) prior; the prior itself is replaced where the
    neighbouring direct anchors agree with each other but not with it."""
    shifts = [r['start_seconds'] - c['start_seconds'] if r['status'] == 'aligned' else None for c, r in zip(cues, rows)]
    keep, prior, decisions = [], [], []
    for i, (c, r) in enumerate(zip(cues, rows)):
        p = prior_starts[i] - c['start_seconds']
        near = [s for s in shifts[max(0, i - cfg.gate_neighbour_cues):i + cfg.gate_neighbour_cues + 1] if s is not None]
        decision = 'prior'
        if len(near) >= cfg.gate_min_anchors:
            med = statistics.median(near)
            mad = statistics.median(abs(s - med) for s in near)
            if mad <= cfg.gate_max_spread_seconds and abs(med - p) > cfg.gate_override_seconds:
                p, decision = med, 'prior_overridden_by_anchors'
        prior.append(p)
        if shifts[i] is None:
            keep.append(r)
        elif abs(shifts[i] - p) <= cfg.gate_direct_tolerance_seconds:
            keep.append(r); decision += '+direct'
        else:
            keep.append({**r, 'start_seconds': c['start_seconds'], 'end_seconds': c['end_seconds'],
                         'status': 'unresolved', 'reason': 'direct_disagrees_with_section'})
            decision += '+direct_rejected'
        decisions.append(decision)
    return keep, prior, decisions


def supported(row) -> bool:
    return row['status'] in ('aligned', 'unchanged')


def resolve_conflicts(source_cues, rows, fallback, min_display=SeqConfig.min_display_seconds):
    """Direct times for aligned cues, fallback times for the rest; conflicts trim padding or revert to fallback.
    source_cues: objects with start/end; rows/fallback: per-cue dicts with start_seconds/end_seconds/status."""
    out = [dict(r) if supported(r) else {**a, 'reason': r.get('reason')} for r, a in zip(rows, fallback)]
    reverted, trimmed = [], []
    for _ in range(len(out)):
        changed = False
        for i in range(len(out) - 1):
            x, y = out[i], out[i + 1]
            allowed = max(0, source_cues[i].end - source_cues[i + 1].start) + .05
            if y['start_seconds'] < x['start_seconds'] - .001 or x['end_seconds'] - y['start_seconds'] > allowed:
                if y['start_seconds'] >= x['start_seconds'] + min_display and allowed <= .05:
                    x['end_seconds'] = y['start_seconds']; trimmed.append(x['cue_index']); changed = True; continue
                for idx in (i, i + 1):
                    if out[idx]['status'] != 'legacy_inferred':
                        out[idx] = {**fallback[idx], 'reason': 'conflict_reverted'}; reverted.append(idx); changed = True
                if not changed:
                    break
        if not changed:
            break
    return out, {'trimmed': trimmed, 'reverted': reverted}


def gated_timings(source_cues, rows, prior_starts, cfg=SeqConfig()):
    """Final v0.2 ('gated') timing map. source_cues need index/start/end/text."""
    cues = [dict(cue_index=c.index, text=c.text, start_seconds=c.start, end_seconds=c.end) for c in source_cues]
    keep, prior, decisions = gate(cues, rows, prior_starts, cfg)
    section = [dict(cue_index=c.index, start_seconds=max(0.0, c.start + p), end_seconds=max(0.0, c.end + p),
                    status='legacy_inferred', reason=d) for c, p, d in zip(source_cues, prior, decisions)]
    final, info = resolve_conflicts(source_cues, keep, section, cfg.min_display_seconds)
    return final, {'decisions': decisions, 'prior_overridden': sum(d.startswith('prior_overridden') for d in decisions),
                   'direct_rejected': sum(d.endswith('direct_rejected') for d in decisions), **info}


def window_stream(windows):
    """One word list on the absolute timeline from overlapping windows. Each word comes only from the
    window whose central region (stride-wide, centred) contains its midpoint, so edge words never count.
    windows: [{'start_seconds', 'duration_seconds', 'words': [{text,start_time,end_time,...} local]}]."""
    wins = sorted(windows, key=lambda w: w['start_seconds'])
    if not wins:
        return []
    stride = statistics.median(b['start_seconds'] - a['start_seconds'] for a, b in zip(wins, wins[1:])) if len(wins) > 1 else wins[0]['duration_seconds']
    out = []
    for k, meta in enumerate(wins):
        o, d = meta['start_seconds'], meta['duration_seconds']
        lo = o + (d - stride) / 2
        hi = lo + stride
        if k == 0:
            lo = float('-inf')
        if k == len(wins) - 1:
            hi = float('inf')
        for w in meta['words']:
            mid = o + (w['start_time'] + w['end_time']) / 2
            if lo <= mid < hi:
                out.append({**w, 'start_time': w['start_time'] + o, 'end_time': w['end_time'] + o})
    return out

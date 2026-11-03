"""One job end to end on the desktop: audio windows -> speech evidence -> sequence map -> ALASS prior -> gate."""
from __future__ import annotations

import subprocess
import time
import uuid
import wave
from pathlib import Path

from .. import timing
from ..protocol import PROTOCOL_VERSION
from ..subtitles import Cue, parse_subtitle

WINDOW_SECONDS = 30
STRIDE_SECONDS = 10


def read_audio(path):
    import numpy as np
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected mono 16 kHz 16-bit PCM audio')
        pcm = audio.readframes(audio.getnframes())
    return np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768.0


def windows(samples, rate=16000):
    """Overlapping windows covering the whole track; every word is later taken from one window's centre."""
    duration = len(samples) / rate
    start = 0
    while start < duration:
        length = min(WINDOW_SECONDS, duration - start)
        if length >= 1:
            yield start, length, samples[int(start * rate):int((start + length) * rate)]
        start += STRIDE_SECONDS


def _stamp(seconds):
    total = round(seconds * 1000)
    hours, total = divmod(total, 3600000)
    minutes, total = divmod(total, 60000)
    secs, millis = divmod(total, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02},{millis:03}'


def _srt(rows):
    return ''.join(f'{i + 1}\n{_stamp(a)} --> {_stamp(b)}\n{text}\n\n' for i, (a, b, text) in enumerate(rows))


def alass_prior(alass, cues, rows, scratch: Path):
    """Section offsets from ALASS, using the directly measured cues as its timing reference.
    Returns per-cue prior starts; falls back to source starts when there are no anchors."""
    anchors = sorted((r['start_seconds'], r['end_seconds']) for r in rows if r['status'] == 'aligned')
    if not anchors:
        return [c.start for c in cues], {'anchors': 0}
    reference, source, output = scratch / 'reference.srt', scratch / 'source.srt', scratch / 'alass.srt'
    reference.write_text(_srt([(a, b, 'speech') for a, b in anchors]), encoding='utf-8')
    # Cue text is irrelevant to ALASS; placeholders keep private dialogue out of its files.
    source.write_text(_srt([(c.start, c.end, f'cue {c.index}') for c in cues]), encoding='utf-8')
    started = time.perf_counter()
    done = subprocess.run([str(alass), str(reference), str(source), str(output), '--disable-fps-guessing',
                           '--split-penalty', '7'], capture_output=True, timeout=600)
    if done.returncode:
        raise RuntimeError('ALASS failed: ' + done.stderr.decode(errors='replace')[-500:])
    shifted = parse_subtitle(output.read_bytes(), 'srt').cues
    if [c.text for c in shifted] != [f'cue {c.index}' for c in cues]:
        raise RuntimeError('ALASS changed cue correspondence')
    return [c.start for c in shifted], {'anchors': len(anchors), 'seconds': round(time.perf_counter() - started, 3)}


def run(claim: dict, audio_path: Path, engine, alass: Path, scratch: Path, heartbeat=lambda: None) -> dict:
    cues = [Cue(c['index'], c['start'], c['end'], c['text']) for c in claim['cues']]
    samples = read_audio(audio_path)
    duration = len(samples) / 16000
    started = time.perf_counter()
    evidence, skipped = [], {}
    for start, length, chunk in windows(samples):
        words, reason = engine.words(chunk)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        evidence.append({'start_seconds': start, 'duration_seconds': length, 'words': words})
        heartbeat()
    speech_seconds = time.perf_counter() - started
    cfg = timing.SeqConfig()
    words = timing.window_stream(evidence)
    rows = timing.map_cues([dict(cue_index=c.index, text=c.text, start_seconds=c.start, end_seconds=c.end) for c in cues],
                           words, cfg, media_duration_seconds=duration)
    prior, alass_info = alass_prior(alass, cues, rows, scratch)
    final, gate_info = timing.gated_timings(cues, rows, prior, cfg)
    reasons = {}
    for row in rows:
        if row['status'] != 'aligned':
            reasons[row['reason']] = reasons.get(row['reason'], 0) + 1
    return {
        'protocol': PROTOCOL_VERSION, 'job_id': claim['job_id'], 'result_id': uuid.uuid4().hex,
        'lease_token': claim['lease_token'], 'subtitle_sha256': claim['subtitle_sha256'],
        'audio_sha256': claim['audio']['sha256'],
        'timings': [{'cue_index': r['cue_index'], 'start': r['start_seconds'], 'end': r['end_seconds'],
                     'status': 'aligned' if r['status'] == 'aligned' else 'legacy_inferred'} for r in final],
        'engine': {**engine.describe(), 'mapper': timing.config_manifest(cfg),
                   'alass': {'split_penalty': 7, **alass_info}},
        'diagnostics': {'windows': len(evidence), 'windows_without_words': skipped, 'asr_words': len(words),
                        'direct_cues': sum(r['status'] == 'aligned' for r in rows), 'unresolved_reasons': reasons,
                        'prior_overridden': gate_info['prior_overridden'], 'direct_rejected': gate_info['direct_rejected'],
                        'conflicts_trimmed': len(gate_info['trimmed']), 'conflicts_reverted': len(gate_info['reverted']),
                        'speech_seconds': round(speech_seconds, 1), 'total_seconds': round(time.perf_counter() - started, 1),
                        **engine.peak_memory()},
    }

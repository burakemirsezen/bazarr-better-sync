"""Local CPU inference candidate and explicit audio selection.

faster-whisper is an optional experiment dependency, not a benchmark-selected
release backend. No media/subtitle content is sent to an external AI service.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile


def probe_media(path: str | Path) -> dict:
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, check=True, timeout=60)
    data = json.loads(result.stdout)
    return data


def media_duration(probe: dict) -> float | None:
    try:
        value = float(probe.get('format', {}).get('duration'))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def choose_audio_track(probe: dict, override: int | None = None) -> int:
    tracks = [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, int) or not any(s.get('index') == override for s in tracks):
            raise ValueError('Selected audio stream does not exist')
        return override
    eligible = []
    for track in tracks:
        tags = {k.lower(): str(v).lower() for k, v in track.get('tags', {}).items()}
        commentary = any(word in tags.get('title', '') for word in ('commentary', 'description', 'descriptive'))
        commentary |= bool(track.get('disposition', {}).get('comment') or track.get('disposition', {}).get('visual_impaired'))
        if tags.get('language') in {'eng', 'en', 'english'} and not commentary:
            eligible.append(track['index'])
    if len(eligible) != 1:
        raise ValueError('Audio selection is ambiguous or English metadata is missing; choose an explicit audio stream')
    return eligible[0]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class FasterWhisperBackend:
    def __init__(self, model: str = 'small.en', threads: int = 2, model_dir: str | Path | None = None):
        if threads < 1:
            raise ValueError('CPU thread count must be positive')
        self.model = model
        self.threads = threads
        self.model_dir = str(model_dir) if model_dir is not None else None
        self._engine = None

    def transcribe(self, media: str | Path, audio_index: int, cache_dir: str | Path) -> list[dict]:
        if isinstance(audio_index, bool) or not isinstance(audio_index, int) or audio_index < 0:
            raise ValueError('Audio stream index must be a nonnegative integer')
        path = Path(media).resolve(strict=True)
        before = path.stat()
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        package_version = importlib.metadata.version('faster-whisper')
        # Cache reuse needs an immutable model identity. Named model aliases
        # may resolve to updated remote weights, so only local model files reuse.
        model_path = Path(self.model)
        immutable_model = None
        if model_path.is_dir() and (model_path / 'model.bin').is_file():
            immutable_model = {p.name: _hash_file(p) for p in sorted(model_path.iterdir()) if p.is_file()}
        identity = {'schema': 1, 'media_sha256': _hash_file(path), 'audio_index': audio_index,
                    'backend': 'faster-whisper', 'version': package_version, 'model': self.model,
                    'model_files': immutable_model, 'device': 'cpu', 'compute_type': 'int8',
                    'language': 'en', 'beam_size': 5, 'word_timestamps': True,
                    'vad_filter': True, 'condition_on_previous_text': False}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = cache_dir / (key + '.json')
        if immutable_model is not None and cache.is_file():
            data = json.loads(cache.read_text(encoding='utf-8'))
            if data.get('identity') == identity:
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError('Media changed during cache lookup')
                return data['transcript']
        if self._engine is None:
            from faster_whisper import WhisperModel
            self._engine = WhisperModel(self.model, device='cpu', compute_type='int8',
                                        cpu_threads=self.threads, num_workers=1, download_root=self.model_dir)
        with tempfile.TemporaryDirectory(prefix='decode-', dir=cache_dir) as directory:
            audio = Path(directory) / 'reference.wav'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', f'0:{audio_index}',
                            '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(audio)],
                           capture_output=True, check=True, timeout=21600)
            segments, info = self._engine.transcribe(str(audio), language='en', beam_size=5,
                                                     word_timestamps=True, vad_filter=True,
                                                     condition_on_previous_text=False)
            transcript = []
            for segment in segments:
                if segment.no_speech_prob > .6:
                    continue
                for word in segment.words or []:
                    if word.end > word.start:
                        transcript.append({'start': word.start, 'end': word.end, 'text': word.word,
                                           'probability': word.probability})
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('Media changed during transcription')
        # Persist reports even for aliases, but only immutable models reuse them.
        descriptor, temporary = tempfile.mkstemp(prefix=key, suffix='.tmp', dir=cache_dir)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump({'identity': identity, 'transcript': transcript}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, cache)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return transcript

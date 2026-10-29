"""Lossless timestamp-only editing of SRT and ASS/SSA source documents."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


def _seconds(value: str) -> float:
    if not re.fullmatch(r'\d+:\d{2}:\d{2}[.,]\d{2,3}', value):
        raise ValueError('Invalid subtitle timestamp')
    hours, minutes, seconds = value.replace(',', '.').split(':')
    if int(minutes) >= 60 or float(seconds) >= 60:
        raise ValueError('Invalid subtitle timestamp')
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _timestamp(value: float, ass: bool) -> str:
    precision = 100 if ass else 1000
    total = round(value * precision)
    hours, total = divmod(total, 3600 * precision)
    minutes, total = divmod(total, 60 * precision)
    seconds, fraction = divmod(total, precision)
    return (f'{hours}:{minutes:02}:{seconds:02}.{fraction:02}' if ass else
            f'{hours:02}:{minutes:02}:{seconds:02},{fraction:03}')


@dataclass
class SubtitleDocument:
    raw: bytes
    source: str
    encoding: str
    format: str
    cues: list[Cue]
    spans: list[tuple[tuple[int, int], tuple[int, int]]]
    relative_tags: bool = False
    bom: bytes = b''

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def render(self, timings: list[tuple[float, float]]) -> bytes:
        if len(timings) != len(self.cues):
            raise ValueError('Exactly one timing pair per source cue is required')
        replacements = []
        for cue, spans, (start, end) in zip(self.cues, self.spans, timings):
            if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
                raise ValueError('Cue times must be finite, nonnegative and increasing')
            if self.relative_tags and abs((end - start) - (cue.end - cue.start)) > .011:
                raise ValueError('Duration changes with ASS animation/karaoke tags are unsupported')
            precision = 100 if self.format != 'srt' else 1000
            if round(end * precision) <= round(start * precision):
                raise ValueError('Cue interval collapses at format timestamp precision')
            for span, value, original in zip(spans, (start, end), (cue.start, cue.end)):
                if abs(value - original) > 1e-9:
                    replacements.append((span[0], span[1], _timestamp(value, self.format != 'srt')))
        result = self.source
        for start, end, value in sorted(replacements, reverse=True):
            result = result[:start] + value + result[end:]
        return self.bom + result.encode(self.encoding)


def parse_subtitle(raw: bytes, format: str = 'srt') -> SubtitleDocument:
    format = format.lower().lstrip('.')
    if format not in {'srt', 'ass', 'ssa'}:
        raise ValueError('Only SRT, ASS and SSA subtitles are supported')
    encoding = 'utf-8'
    bom = b''
    if raw.startswith(b'\xef\xbb\xbf'):
        bom = b'\xef\xbb\xbf'
    elif raw.startswith(b'\xff\xfe'):
        encoding, bom = 'utf-16-le', b'\xff\xfe'
    elif raw.startswith(b'\xfe\xff'):
        encoding, bom = 'utf-16-be', b'\xfe\xff'
    try:
        source = raw[len(bom):].decode(encoding)
    except UnicodeDecodeError:
        if bom:
            raise ValueError('Malformed Unicode subtitle')
        encoding = 'cp1252'
        source = raw.decode(encoding)
    cues, spans = [], []
    if format == 'srt':
        pattern = re.compile(r'(?m)^(\d{2,}:\d{2}:\d{2}[,.]\d{3})([ \t]+-->[ \t]+)(\d{2,}:\d{2}:\d{2}[,.]\d{3})([^\r\n]*)(?:\r?\n|$)')
        matches = list(pattern.finditer(source))
        for index, match in enumerate(matches):
            boundary = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            text = source[match.end():boundary]
            text = re.split(r'\r?\n[ \t]*\r?\n', text, maxsplit=1)[0].rstrip('\r\n')
            cues.append(Cue(index, _seconds(match[1]), _seconds(match[3]), text))
            spans.append((match.span(1), match.span(3)))
        if source.count('-->') != len(matches):
            raise ValueError('Malformed or unsupported SRT timestamp line')
    else:
        fields = None
        in_events = False
        for match in re.finditer(r'(?m)^([^\r\n]*)(?:\r?\n|$)', source):
            line = match[1]
            if line.strip().startswith('['):
                in_events = line.strip().lower() == '[events]'
                fields = None
            if not in_events:
                continue
            if line.lower().startswith('format:'):
                fields = [f.strip().lower() for f in line.split(':', 1)[1].split(',')]
                if not all(f in fields for f in ('start', 'end', 'text')) or fields[-1] != 'text':
                    raise ValueError('Unsupported ASS/SSA event format')
            if line.lower().startswith('dialogue:'):
                if fields is None:
                    raise ValueError('ASS/SSA events require an explicit Format line')
                body = line.index(':') + 1
                parts = line[body:].split(',', len(fields) - 1)
                if len(parts) != len(fields):
                    raise ValueError('Malformed ASS/SSA dialogue')
                offsets, cursor = [], body
                for part in parts:
                    offsets.append((match.start(1) + cursor + len(part) - len(part.lstrip()),
                                    match.start(1) + cursor + len(part.rstrip())))
                    cursor += len(part) + 1
                start_i, end_i = fields.index('start'), fields.index('end')
                cues.append(Cue(len(cues), _seconds(parts[start_i].strip()), _seconds(parts[end_i].strip()), parts[fields.index('text')]))
                spans.append((offsets[start_i], offsets[end_i]))
    if not cues:
        raise ValueError('No supported subtitle cues found')
    if any(c.start < 0 or c.end <= c.start for c in cues):
        raise ValueError('Invalid subtitle cue interval')
    relative_tags = bool(re.search(r'\\(?:[kK][fot]?\d|t\s*\(|move\s*\(|fad[e]?\s*\()', source))
    return SubtitleDocument(raw, source, encoding, format, cues, spans, relative_tags, bom)


def load_subtitle(path: str | Path) -> SubtitleDocument:
    path = Path(path)
    return parse_subtitle(path.read_bytes(), path.suffix)

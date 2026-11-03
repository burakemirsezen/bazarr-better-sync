# Sequence mapper v0.2: concept evidence, 2026-11-03

This document is the Gate A evidence for the timing method implemented in `bazarr_sync.timing` and run by the desktop worker. Full reports, per-cue maps and review clips are kept outside Git with the private media. No subtitle dialogue appears here.

## Method

1. **Speech evidence.** Qwen3-ASR 1.7B transcribes overlapping 30 s windows with a 10 s stride. The Qwen3 forced aligner (0.6B) times each word. Both models run on the RX 7800 XT through ROCm in float16, at the pinned revisions in `worker/gpu.py`. Each word is taken only from the window whose central 10 s contains it.
2. **Sequence mapping.** rapidfuzz Levenshtein opcodes align the full subtitle token stream to the full ASR token stream. Inside edited regions, the mapper pairs hyphen/compound splits and fuzzy words.
   - A cue becomes a direct anchor when at least 2 tokens and 60% of its tokens match, and no unclaimed identical copy exists nearby.
   - Cues of 1–2 tokens must match exactly and be unique between accepted neighbours.
   - A cue boundary is measured only when that end's word was matched with reliable timing.
3. **Section prior.** ALASS 2.0.0 (split penalty 7) shifts the source cues against the direct anchors.
4. **Gate.**
   - Where at least 3 nearby anchors agree with each other (spread ≤ 0.25 s) but differ from ALASS by more than 0.5 s, their median replaces the prior.
   - A direct time is kept only within 0.75 s of its section.
   - Every other cue takes its section shift.
   - Conflicts trim padding or revert, so no new overlaps or inversions appear.

Cue wording, order and formatting are never changed.

## Results

**Synthetic controls with known truth.** Development used two control sets. A third set was held out: new sentences, schedule and voice, hash-frozen before any model saw it. On every case of the held-out set, the method made 0 harmful changes (> 100 ms worse than the source) and 0 false certifications of absent or non-speech cues, with 22–26 ms endpoint error. It covers already-correct timing, independent start/end errors, a 4 s section offset, an audio-only prefix, a missing utterance, absent wording, an SFX cue and an isolated repeat. Plain ALASS harmed 4 cues on independent boundary errors and reached 256 ms error on the missing-utterance case.

**Real episodes (The Bear S01, one release).** The original Bazarr subtitles are offset section by section, roughly +4 s → +2–3 s → 0 → −2 s. One global offset cannot fix them.

| Episode | Role | Direct anchors | Outcome |
| --- | --- | ---: | --- |
| E01 | Development | 424 / 499 | Owner confirmed the previously failing transition and a newly detected −2 s ending |
| E03 | Development | 376 / 453 | Four clean sections |
| E02 | Held out | 479 / 532 | Owner preferred v0.2 over plain ALASS in two blind A/B comparisons |
| E04 | Held out | 430 / 490 | Accuracy check below |

**Accuracy against human marks (E04).** The owner blindly marked the speech onset of 30 random spoken cues:

| Version | Median error | Within 0.25 s | Within 0.5 s | Over 1 s |
| --- | ---: | ---: | ---: | ---: |
| Bazarr subtitle as downloaded | 1981 ms | 17% | 17% | 25 |
| Plain ALASS | 234 ms | 50% | 87% | 0 |
| **Sequence mapper v0.2** | **130 ms** | **67%** | **87%** | **0** |

The 130 ms median includes the deliberate 0.1 s caption lead. Marking precision is about ±50–100 ms.

**Where the remaining error comes from.**
- Cues measured at both ends: median 81 ms.
- Section fill: median 322 ms.
- Cues whose start is inferred as end − original duration: median 484 ms.

Two candidate v0.3 changes:
- Take the start from the section shift whenever only the end is measured.
- Give both-ends-measured cues a wider gate tolerance.

**Cost.** About 6–7 minutes of GPU time per 28-minute episode, about 9.6 GB peak VRAM. Mapping, ALASS and the gate take well under 1 s.

## Limits

- Evaluated on one series and one release, in English only.
- Turkish carry-over is unverified.
- These 30 human marks are now development data. A fresh marked episode is needed to measure any v0.3 change.

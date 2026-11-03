# GPU timing concepts — 2026-10-31

Status: isolated concept evidence, **not a production release or proof of natural-episode timing accuracy**. Live repair remains stopped. Experiments did not publish subtitles or change Bazarr settings. See [transition plan](transition-plan.md) and [SPEC.md](../SPEC.md).

## Execution established

The Windows AMD Radeon RX 7800 XT successfully executed HIP tensor operations and the following local models in an isolated Python 3.12 environment. No host driver or global GPU configuration was changed.

- Qwen3-ForcedAligner-0.6B-hf, revision `c07281df297b9905d24a508279258cccf987a064`.
- Qwen3-ASR-1.7B-hf, revision `bcd2b5b7f32b480ab5790554cfa8347f246a14f3`.
- Transformers commit `66f51e76efd8542232b8bdd40ade7d1edbaac46f` and AMD torch `2.15.0+rocm10.3.0a20261030`, float16/eager attention.

A mixed torch installation initially failed DLL loading; a clean AMD-only reinstall fixed it. A successful resolver/install alone would not have established compatibility.

## Timing concepts compared

| Concept | Finding |
| --- | --- |
| Force-align supplied subtitle text directly | Accurate matching synthetic speech, but falsely assigned plausible times to all four unsupported negative cues. Rejected as a standalone method. |
| Remove unsupported subtitle text, then align remaining text | Left actual speech unrepresented and distorted supported neighbors by several seconds. Rejected. |
| Align the complete unprompted ASR transcript, then transfer supported passages to original cue identities | Removed the observed synthetic neighbor contamination; conservative unmatched/ambiguous cues remained unchanged. Promising mechanism; natural quality remains unproven. |

Support matching, ambiguity rejection, local smoothing guards and a 0.12-second retain-source policy are experimental heuristics, not calibrated confidence. An independent evaluator checked source/audio identities, unsupported certifications, supported-neighbor errors and raw versus policy-modified timings. Complete-ASR transfer must not be confused with rewriting the authored subtitle text.

## Bounded results

Eight controls were generated from one scheduled local-TTS recording. They covered independent start/end/gap errors, correct timing, an exact two-second prefix, absent wording, paraphrase, a subtitle-only sound effect, removed dialogue and repeated ambiguity. These controls are not eight independent natural recordings or a held-out production corpus.

For the controlled positive, complete-ASR transfer with the retain-source policy disabled produced approximately 24.94 ms mean endpoint error and 56.44 ms maximum error. Supported-neighbor maximum error in the removed-dialogue case was 62.5 ms. Authored non-timing content survived rendering. Exact preservation of the already-correct control with the policy enabled must be attributed partly to that policy, not presented as zero raw model error. The aligner's timestamp bins are 80 ms.

A roughly 52-second forced-alignment input took about 14–22 seconds on first-shape/cold execution and about 0.17 seconds for a warm forward pass. Peak model GPU allocation was about 2.26 GB. These are synchronized forward measurements, not end-to-end episode latency. The larger ASR model used about 4.55 GB peak allocation in the real excerpt tests; warm 60-second excerpt runs took about 6.0–7.5 seconds. Desktop applications, allocator reservation, model loading, extraction and transfer add costs.

Three separated 60-second real-episode excerpts yielded proposals for 25 of 40 cues; the other cues were retained. Median proposed start shifts varied by region, approximately +3.73, +1.92 and +0.09 seconds. Those are proposed shifts, **not measured timing errors against human truth**. Some raw real-audio alignments required very large smoothing, so plausible selected cues do not validate the whole excerpt or episode.

## Gate decision

The GPU execution path and per-cue timing/gap mechanism are feasible. The first two alignment concepts failed meaningful negative tests. The third warrants further evaluation, but Gate A remains incomplete: no independently human-reviewed natural references or natural already-correct controls were available, and the small synthetic set informed iteration. No automatic-publication release criteria were established.

Do not build the full distributed application or resume live repair on these results alone. Next compare the surviving concept on held-out controls and independently reviewed natural excerpts, audit temporal correspondence and overlaps, and evaluate alternative models if errors remain. Models are not selected permanently by this experiment.

Private source media, dialogue, transcripts and infrastructure paths are excluded from this repository. Detailed local experiment artifacts remain outside Git. Transient audio/transcripts/build caches are distinct from intentionally reusable model/runtime assets and durable rollback backups.

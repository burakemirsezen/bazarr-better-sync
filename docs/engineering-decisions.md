# Engineering decisions

Updated 2026-10-31. [SPEC.md](../SPEC.md) defines the target; [transition-plan.md](transition-plan.md) defines the evidence gates. Decisions below distinguish existing code from the planned replacement architecture.

## Current direction: coordinator plus desktop GPU worker

The owner requested optional Advanced Sync with a persistent Kubernetes producer/coordinator and a desktop UI/GPU consumer. The desktop preserves downloaded text and returns cue timings; the coordinator validates and publishes. The earlier CPU-first worker inside the server is no longer the target architecture. No distributed worker or GPU backend has been implemented in this checkout.

The desktop target identified during local inspection is an AMD Radeon RX 7800 XT. The cluster's Intel graphics are a different device and do not constrain the desktop design. Model/engine/precision selection remains open. The owner authorized larger compatible models and substantial local compute, with concept validation before the full application. A Vulkan engine is one possible experiment, not a selected dependency or measured result.

Local evidence motivates per-cue/section-aware alignment. One real episode required different offsets in three regions; a separate subtitle source was truncated and needed explicit source recovery. Short activity-only samples gave inconsistent offsets. These observations do not establish model accuracy, but show why a single global shift and an assumption of complete input are insufficient.

A full-episode CPU small.en experiment took about 629 seconds for about 1,667 seconds of audio, with roughly 1.55 GiB peak RSS. Earlier global matching abstained on real variants. Abstention means the candidate did not establish a safe correction; it does not prove the input was correct or ffsubsync sufficient. Controlled transformations and ASR-derived checks are not independent human timing truth.

Live repair is paused at the owner's request. Existing changes and recovery backups remain; no experiment may publish further library changes. Private repair receipts and dialogue are kept outside Git. The season is not declared fixed.

## Implemented prototype foundation

Python 3.11+ and the standard library implement the API, web UI, catalog and SQLite queue. One application process owns one persistent data directory using OS locking. Submission acknowledgements follow committed transactions; interrupted local work is surfaced for explicit retry. These local job states do not implement distributed attempts, leases or worker authentication.

Bazarr discovery uses its HTTP API with an API-key header. Import records metadata only. Missing, embedded, non-English and unsupported subtitles are ineligible. Explicit selection is the default workflow; the optional submission client is not installed or enabled automatically.

The original inference adapter uses CPU INT8 faster-whisper and a global offset/drift matcher. Its guards reject sparse, ambiguous or inconsistent evidence; they are experimental thresholds, not calibrated probabilities. Keep it as a comparator while testing better methods. The current source cannot be described as a working cue-level GPU aligner.

Automatic model publication is disabled. Manual anchors require preview and explicit Apply. The file layer retains exact originals and recovery metadata, checks source versions, renders timestamps without intentional content changes, and provides guarded rollback. External filesystem writers do not honor application locks; documented rechecks reduce but cannot eliminate that race.

## Evaluation work retained

The timing-reference harness binds private sparse references to subtitle/media identity and a declared audio stream. Human truth fields start empty and require explicit review attestation. Reference points are scoring input only, not fitting anchors. Coverage describes reviewed cue fraction and time distribution; starts and ends may be reviewed independently.

Candidate/baseline comparison checks rendered timestamp precision and preservation of non-timing content, including ASS/SSA metadata, encoding and BOM. A changed document is noncomparable rather than silently indexed. ffsubsync receives a scratch input copy and explicit audio reference when selected. Output files are created exclusively to avoid overwriting source/reference/report inputs. These safeguards and tests remain useful for GPU experiments.

Model selection must use independent real timing references and already-correct controls in addition to synthetic cases. Report timing quality, harmful changes, coverage/abstentions, preservation and resource costs separately. The Bear failures are not correct controls. No selected production model or numeric automatic-publication release gate exists.

## Target engineering boundaries

Keep SQLite and one coordinator; add versioned state migration, attempts/leases, source/artifact identities and immutable proposals rather than add a required broker. Protocol endpoints and implementation details are still to be designed. Use outbound desktop connections; no writable desktop library mount or Bazarr secret is needed.

Return a timing map tied to the exact source, not an authoritative rewritten subtitle. Server rendering keeps file preservation and publication centralized. Per-cue starts/ends allow new gaps; unchanged and unresolved cues remain explicit. Context models may assist matching but must not rewrite authored words silently.

The old unauthenticated internal API is not suitable for remote worker exposure. Define authenticated private transport, worker-scoped access, revocation and a separate operator session before cross-machine integration. Authentik remains the browser boundary. No live ingress/network configuration change follows from this document.

## Packaging and unresolved choices

The current Dockerfile is an experimental CPU image with FFmpeg and optional ASR. Linux amd64 has prior benchmark evidence; other architectures and desktop packaging remain unverified. The top-level ASR pin is not a full reproducible dependency lock. Production registry, license, numeric resource limits, retention and automatic-publication criteria remain open.

Models may be intentionally retained in a declared reusable local cache. Audio extracts, transcripts, incomplete transfers and experiment scratch require owned paths, finally cleanup and verification after cancellation/failure. Recovery backups are durable user data and must not be treated as disposable benchmark files.

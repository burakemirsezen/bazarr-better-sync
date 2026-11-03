# Transition plan: prove timing repair, then distribute it

Updated 2026-10-31. Status: initial isolated concepts tested; no distributed implementation yet. Gate A remains incomplete; see [GPU concept results](benchmark-gpu-concept-2026-10-31.md). [SPEC.md](../SPEC.md) is authoritative.

## Immediate state

Live season repair is stopped. Its temporary worker/job and repair scratch have been removed; original recovery backups and applied changes stay intact. The season is not declared fixed. The architecture work and subsequent concept experiments have no authority to publish further library changes.

Keep the existing source/tests as the experimental CPU/manual baseline. Retain the independent timing-reference evaluation tooling. Do not merge a separate saved prototype wholesale or assume its version label is a production release. Inspect useful section-alignment/cancellation improvements individually when they are needed.

## Gate A — model and alignment concepts

Run bounded experiments before building the coordinator protocol, desktop application or packaging. Research currently available engines/models using primary documentation, then test actual Windows/AMD execution. The local target is the RX 7800 XT; inspect live VRAM/free resources rather than infer that a named GPU backend supports it. A large model is welcome when it fits. Current faster-whisper/small.en choices are baselines, not constraints.

Compare at least these strategies when compatible candidates exist:

- Coarse speech/text search followed by local cue alignment, with short separated windows and expansion where evidence disagrees.
- Full selected-audio transcription followed by subtitle matching and boundary refinement.
- Direct/forced alignment using downloaded text, with explicit handling for paraphrases and non-speech cues.

Research may reject a strategy before running it when supported evidence shows incompatibility or inappropriate assumptions. Record why. Keep transcription and boundary alignment distinct: a model producing better words may still produce poor caption boundaries.

Use isolated scripts/adapters, read-only library inputs, explicitly provisioned model caches, and temporary audio/proposals. Do not modify the application queue/API to fit a model before its timing approach works. Do not send media to hosted inference. No automatic downloads of every candidate; shortlist first and account for model/disk/VRAM cost.

### Required evidence

1. Confirm GPU execution rather than accidental CPU fallback; record engine/model revision, precision, decoding/alignment settings, device, VRAM/RAM, input/audio identity and cold/warm runtime.
2. Obtain independent cue start/end references spanning early/middle/late regions and timing discontinuities. The Bear failures are useful evaluation cases, but cannot substitute for already-correct controls or human truth. If trustworthy references are unavailable, mark the quality gate incomplete.
3. Compare the original and recorded Bazarr/ffsubsync baseline with timing-map proposals. Include offset, section/cut and already-correct cases. Hold out regions/cases from tuning.
4. Report cue boundary error and its distribution, harmful changes, unmatched cues, temporal coverage and preservation separately. Explain failures and whether a short-window approach missed later timing changes.
5. Demonstrate cue-specific timings and gaps, exact wording/style preservation, unchanged uncertain regions, and explicit detection of incomplete/incompatible sources.
6. Leave a reproducible report, a small synthetic/public-safe fixture where useful, exact commands/configuration, and cleanup receipt. No private media, subtitle dialogue, credentials or full transcripts enter Git.

Gate A exits with a recommended concept plus evidence and limitations, or an explicit inconclusive/no-go result and next experiment. Do not claim the full season fixed, calibrated confidence, or production readiness. Owner-agreed numeric release criteria remain separate from concept selection.

## Gate B — minimal end-to-end coordinator/worker slice

Start only when Gate A is convincing. First freeze a small versioned contract with fake-worker fixtures, then integrate the proven alignment adapter.

- Persist selected jobs, attempts, leases, source/artifact identities and immutable timing proposals in the coordinator. Keep one server instance and SQLite; no new broker.
- Prepare/checksum scoped audio with exact mapping to media time. Worker requests use artifact IDs, never arbitrary filesystem paths.
- Implement outward desktop polling/claim, heartbeat, bounded transfers, result validation, idempotency, cancellation and offline/retry behavior.
- Add authenticated private worker access with scoped revocable credentials and a separate operator session for review/publication. Do not expose the existing unauthenticated prototype API remotely.
- Render timing maps on the server, validate original content and stale sources, and store a proposal without applying it.

Prove one read-only real-media round trip, plus disconnect, restart, expired lease, duplicate/conflicting/stale results and cancelled-job rejection. Leave no orphan audio/processes. A mock protocol success alone does not replace Gate A's quality evidence.

## Gate C — desktop UI and guarded publication

Build the UI around the proven flow: connect/GPU status, catalog selection, Advanced Sync, progress, pause/cancel/retry, audio choice, timing diff and unresolved cues. Retain usable web review infrastructure without creating a second queue. No embedded video player is required.

Use immutable proposal IDs for review/Apply. Reuse and harden the existing lossless render, exact backups, atomic replacement, readback and guarded rollback. Cover source changes during queueing/inference/review and external-writer limits. Begin publication verification on disposable files. Any return to live season repair is a separate explicit user instruction.

## Gate D — packaging and optional rollout

Only after quality and failure handling pass: package the Windows desktop worker and coordinator image; document model provisioning, resource limits, private connectivity, pairing/revocation, startup cleanup, state migration, retention and recovery. Owner maintains Kubernetes resources. Test cold installation and unclean shutdown without GPUs in the coordinator.

Manual review remains the default. Automatic queueing via Bazarr hook and automatic publication need separate opt-ins; automatic publication also requires agreed release criteria. Do not enable either during installation or benchmarking.

## Reusable foundation and implementation gaps

| Existing in this checkout | Transition treatment |
| --- | --- |
| Bazarr API catalog and explicit selection | Reuse; preserve metadata-only import and eligibility boundaries. |
| SQLite queue and deduplication | Extend with versioned migrations, attempts/leases and immutable proposals. Current local `running` state is not a remote lease. |
| SRT/ASS/SSA parser/render and backups/rollback | Reuse after cue-map and stale-proposal validation; server remains the only writer. |
| Manual anchors and web UI | Keep as baseline/review tools; desktop workflow is new. |
| CPU faster-whisper/global offset/drift matcher | Retain comparator; not the selected future engine or per-cue alignment solution. |
| Sparse timing-reference scoring | Reuse independent references, coverage and preservation checks; add GPU/resource and per-cue diagnostics as needed. |
| Docker/CI and disposable cluster benchmark tools | Keep experimental; not a deployed coordinator/desktop product. |

## Subagent handoff boundaries

Assign model/backend research and isolated concept benchmarking before full product implementation. Agents may use substantial local GPU compute and larger compatible models; preserve resource measurements and compare alternatives rather than accept the existing model by default. Share a single explicitly owned GPU experiment at a time unless measured headroom supports more. Serialize large downloads/compute to avoid contention.

Each handoff must state input scope, read-only/no-Apply restriction, model/cache/scratch ownership, expected evidence, cleanup and stop condition. Preserve downloaded models in a declared reusable cache when intentionally provisioned; delete transient extracts/transcripts and abandoned partial downloads. Return reports and proposed implementation work for review before Gates B–D proceed.

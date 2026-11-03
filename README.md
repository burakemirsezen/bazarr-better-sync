# Bazarr Better Sync

Optional subtitle timing repair for upstream Bazarr. The target design uses a **Kubernetes coordinator and a desktop GPU worker**: the server records selected jobs and owns subtitle files; the desktop computes timing proposals while preserving the downloaded wording.

**Status: architecture transition; proof of concept comes first.** The distributed worker, pairing, desktop application, and GPU backend are not implemented in this checkout. The existing Python service is an experimental CPU/manual prototype, retained as reusable infrastructure and evaluation tooling. No production release or real-media accuracy claim is made.

## Intended workflow

1. Import Bazarr's catalog without analysing or modifying files.
2. Select an episode, movie, or season and request **Advanced Sync**.
3. A connected desktop worker claims the job and processes the selected audio locally on its GPU.
4. It returns cue start/end times, evidence, and unresolved regions. The server renders these times onto the original subtitle document.
5. Review the proposal and explicitly Apply. The server checks source versions, preserves an exact original, replaces atomically, and supports guarded rollback.

The worker may rebuild individual cue timings and gaps. A single offset or global stretch is sufficient only when the evidence supports it. Subtitle generation, translation, and silent wording cleanup are outside this workflow. Normal Bazarr downloads do not wait for the desktop, and installing the companion must not enable a download hook.

The initial desktop target is Windows with an AMD Radeon RX 7800 XT. Model/backend selection is open. Larger models and substantial local compute are allowed when this GPU can run them; compatibility, timing accuracy, memory use, and runtime must be measured before selection. Inference remains local.

## Start here

- [Product and engineering specification](SPEC.md): authoritative target architecture and boundaries.
- [Transition plan](docs/transition-plan.md): concept experiments, acceptance evidence, and staged implementation.
- [Engineering decisions](docs/engineering-decisions.md): implemented prototype choices and superseding direction.
- [Initial GPU concept results](docs/benchmark-gpu-concept-2026-10-31.md): execution is feasible; real-quality gate remains incomplete.
- [Prototype operations](docs/operations.md): commands and API that actually exist today.
- [Timing-reference workflow](docs/timing-reference-workflow.md) and [historical CPU benchmark](docs/benchmark-2026-10-29.md).

The first gate is convincing real-media evidence, including independent reference timings and already-correct controls. Controlled offsets alone cannot establish quality. Automatic publication remains disabled pending representative evaluation and owner-agreed release criteria.

## Existing prototype

Python 3.11+ and FFmpeg/ffprobe are required. The base application has no third-party Python runtime dependencies. See [operations](docs/operations.md) for persistent state, media mappings, Bazarr credentials, and access restrictions. Use `BBS_BACKEND=disabled` for catalog/manual operation; optional `faster-whisper` inference is an experimental CPU baseline.

```sh
python -m pip install .
python -m bazarr_sync
python -m unittest discover -s tests -v
```

Media roots default to empty and must be configured explicitly. The current web service has no built-in authentication; protect it behind owner-managed Authentik and restricted networking. Its existing endpoints are **not** the future desktop worker protocol. No desktop worker connection should be exposed through them.

The repository retains subtitle parsers, queue/catalog code, file preservation and rollback logic, UI, Docker/CI, and benchmark tooling for evaluation and reuse. No license, production image registry, retention policy, or distributed release has been selected. Kubernetes deployment remains owner-maintained.

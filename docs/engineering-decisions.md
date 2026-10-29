# Initial implementation decisions

This is an experimental first implementation, not a production release. The owner approved routine engineering choices in SPEC.md. No software license, image registry, retention period, production resource budget, or numeric release criteria have been selected.

## Application

Python 3.11+ keeps the application next to the speech and subtitle tooling. The initial API, web UI, persistent catalog, and queue use the standard library. SQLite with committed transactions provides durable submission acknowledgements within one application instance, without an external database or queue. Run exactly one application process against its data directory. One worker runs one analysis at a time. Interrupted work is reported for explicit retry; it is not silently treated as complete.

The UI uses server-hosted HTML and JavaScript, with no external CDN, analytics, or media streaming. Bazarr discovery uses its HTTP API with the key in a header. Import writes catalog metadata only. Missing, embedded, non-English, or unsupported-format subtitles are ineligible for processing. No automatic whole-library assessment is scheduled.

## Alignment candidate and release boundary

The first candidate matches distinctive subtitle text against timestamped local speech recognition, estimates a global offset/drift transformation, and checks held-out matches. It rejects inadequate coverage, ambiguity, non-monotonic matches, inconsistent residuals, and unsupported timing changes. Thresholds are conservative experimental guards, not calibrated probabilities. Cuts and abrupt scene differences can abstain; robust section alignment remains further work.

The optional inference adapter uses faster-whisper on the CPU with INT8. This is a candidate chosen for evaluation, not a claim of superior quality. Upstream documents CPU INT8 and timestamped output: [faster-whisper](https://github.com/SYSTRAN/faster-whisper). The baseline is the ffsubsync version installed in Bazarr. Keep model names, decoding parameters, source identity, and timing references in benchmark reports.

Automatic replacement is deliberately not enabled in this experimental build. Model output becomes a proposal requiring review. The product requirement for automatic validated corrections remains: enable it only after representative real-media evaluation and owner-agreed release criteria. Manual anchors produce a preview and require explicit Apply. File replacement and rollback retain version checks and backups.

## Packaging and operations

The Dockerfile provides a CPU-only image with FFmpeg. Speech inference is an optional build extra, avoiding mandatory model/dependency downloads for catalog and manual workflows. A model is downloaded only when its inference backend is used; provision a persistent model cache. The initial image recipe uses a Python 3.11 Debian base and a pinned top-level speech package. It is not a fully locked, digest-reproducible dependency supply chain; establish image digests and a complete dependency lock before a production release. Linux amd64 is the benchmark platform; other architectures are unverified.

The process runs as UID/GID 10001. The deployment owner must grant that identity access to persistent state and selected subtitle destinations. No chown or permission changes to the media library are performed by the application. Retention is manual in this prototype; no backup/cache/report deletion policy is silently selected.

The latest request authorizes temporary cluster benchmarking through kubectl. It does not select production deployment resources. Benchmark scripts may create bounded disposable test resources with read-only library mounts; these are tooling, not application deployment deliverables. Home-Infra production configuration, Bazarr post-processing settings, ingress, and Authentik are not changed by this implementation.

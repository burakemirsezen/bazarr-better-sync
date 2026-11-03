# Operating the existing CPU/manual prototype

> Architecture transition (2026-10-31): this document describes commands, configuration and API already present in the checkout. The target is a Kubernetes coordinator plus desktop GPU worker; see [SPEC.md](../SPEC.md) and the [transition plan](transition-plan.md). Update 2026-11-03: the `remote-gpu` backend adds token-authenticated worker endpoints under `/worker/v1/`, leases, audio artifacts and GPU execution on the desktop; see [gpu-worker.md](gpu-worker.md). Expose only `/worker/v1/` to workers. Never expose the operator API below as a remote worker interface. Live repair is paused; concept benchmarks must use read-only inputs and scratch proposals.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `BBS_DATA_DIR` | `./data`; image `/data` | Writable persistent SQLite state, backups, model files, transcript cache. |
| `BBS_MEDIA_ROOTS` | `[]` | JSON array of permitted local roots. Empty fails closed for job files. |
| `BBS_PATH_MAPPINGS` | `{}` | JSON object mapping Bazarr prefixes to companion mount prefixes. Longest boundary-matching prefix wins. |
| `BBS_BAZARR_URL` | empty | Internal Bazarr base URL, without `/api`; import requires this. |
| `BBS_BAZARR_API_KEY` | empty | Bazarr API key, sent via `X-API-KEY`. Keep out of source control. |
| `BBS_HOST` | `0.0.0.0` | Listener interface; use `127.0.0.1` for a local-only source preview. |
| `BBS_PORT` | `8080` | Listener port. |
| `BBS_PUBLIC_ORIGIN` | empty | Exact browser origin, e.g. `https://sync.example.org`, when behind a proxy. |
| `BBS_BACKEND` | `disabled` | `disabled` for manual review or `faster-whisper` for experimental CPU inference. |
| `BBS_MODEL` | `small.en` | Candidate model name or immutable local model directory. No production model selected. |
| `BBS_THREADS` | `2` | Experimental inference thread count, 1–128. Not an owner-approved resource budget. |

One application instance owns a data directory, enforced by an OS file lock. Use a local/block filesystem with working SQLite WAL and locking semantics for `/data`; do not place SQLite on shared NFS. Media may use shared storage. Run one process/replica. Back up the entire data directory before upgrades. No development-schema compatibility guarantee is offered yet.

The container uses UID/GID 10001. Grant that identity access through deployment ownership/groups; do not indiscriminately chown the library. Replacing a subtitle requires writes to its parent directory. Read-only mounts are appropriate for discovery and model previews. Source operation needs ffprobe even in manual mode. Models and transcripts stay local; download hosts receive model requests, not media/subtitles.

## Integration behavior

Use explicit item selection in the companion UI as the default optional timing-improvement workflow. Bazarr saves subtitles normally; companion analysis runs later for the items you request. Installing the service does not install a download hook. Keep that integration unconfigured unless you deliberately opt in to queueing new downloads.

The companion's queue is independent of normal download completion and playback. An accepted job is optional analysis, and any publication still follows the configured review/validation policy with backups and rollback.

Discovery pages movies and series, then requests episodes per series. The installed Bazarr contract was inspected during benchmarking; the connector uses its `seriesid[]` episode parameter. Import updates present/missing metadata and never creates jobs. Embedded, non-English, and unsupported-format subtitles are catalog-only. Eligibility is not a timing assessment. Processing starts only for explicitly selected IDs or an explicit download-hook submission.

The standalone `submit.py` needs Python standard library only. It accepts `--url`, `--video`, `--subtitle`, `--language`, `--audio-index`, and `--timeout`. The default timeout is 10 seconds, capped at 60. A 202 response acknowledges committed queue state, not alignment. Repeated identical input versions return the same job ID. Explicit retry creates a separate retry identity. Media metadata and subtitle hashes are captured on submission and rechecked before processing/publication. Changed inputs require fresh submission. HTTP redirects are refused.

Configure Bazarr's post-processing command only after checking script paths and mount mappings on scratch files. Insert the installed version's documented video/subtitle substitutions; do not send a subtitle path as the video argument. If eventually using this companion as automatic alignment authority, avoid stacking Bazarr's built-in automatic sync and review hook score thresholds. This prototype does not provide automatic replacement and should not replace production sync yet. It does not modify Bazarr settings, intercept the manual Sync button, or refresh media servers after asynchronous completion.

## Jobs, previews, and files

Queued submissions persist. Jobs left running at restart become `interrupted` and require explicit retry. Each explicit retry creates fresh work, including a second retry from the same original job; ordinary duplicate download-hook submissions still deduplicate. If saving a worker outcome fails, the worker stops and readiness stays unhealthy until a process restart reconciles interrupted work. Failures and uncertain outcomes preserve active subtitles. Choose an explicit audio stream when multiple English tracks exist or metadata is missing; commentary/descriptive tracks are excluded from automatic selection. The user can override audio in a retry.

Manual anchors specify the intended **start** of a zero-based cue. One anchor applies an offset. Multiple anchors use piecewise linear interpolation; outside that range the nearest anchor offset is retained. Anchors do not prove that intervening cuts match. The preview validates finite ordered anchors, media bounds, cue intervals, timestamp precision, and format constraints. The UI shows up to 500 cues; output preserves the complete document. Manual preview refuses missing or invalid ffprobe duration metadata so media bounds cannot be silently skipped.

Apply requires a successful manual preview and rechecks input versions. It stores an exact original backup and recovery metadata before atomic replacement. Rollback requires the active file to still match the applied version, validates backup integrity, and backs up the replaced output too. Timestamp editing preserves BOM/endian, text, styles, event fields, and intentional overlaps. Duration changes with ASS/SSA karaoke/animation tags are rejected. Unsupported/malformed inputs fail instead of silently converting to SRT.

Application locks serialize its own writers. Bazarr and external editors do not honor them; portable filesystems lack atomic hash-conditional rename. Avoid concurrent upgrades/edits during Apply. Rechecks narrow this race but cannot eliminate it. After a crash during publication, inspect `/data/backups/*.json` and current hashes to recover: prepared metadata survives even if the HTTP operation did not complete. A stale `.bazarr-sync.lock` beside a subtitle requires checking no writer is active before manual removal. No automatic lock stealing occurs.

Backups, reports, transcript cache, and models have **no automatic retention/deletion** in this prototype. Monitor free space. A mono 16kHz WAV needs about 115 MB/hour plus model/transcript storage. CPU/memory limits and maximum media duration remain owner choices. Extraction has a six-hour safety timeout; there is no in-flight inference cancellation or bounded application job deadline yet. Do not enqueue the whole library to discover performance limits.

## API and access

There is no built-in login or API authentication, as requested. Direct service access grants operations including manual Apply and rollback. Protect browser ingress with Authentik and restrict internal connectivity. Set `BBS_PUBLIC_ORIGIN` to the external browser origin. Arbitrary forwarded identity headers are not authorization. No CORS is enabled. Browser POST requests require JSON, a custom header, and an accepted Origin; machine clients can omit Origin.

| Method | Path | Body / response |
| --- | --- | --- |
| GET | `/healthz` | Process health and backend. |
| GET | `/readyz` | Worker liveness, not inference-quality certification. |
| GET | `/api/catalog` | `{data: [...]}` imported targets. |
| GET | `/api/jobs` | Up to 500 recent jobs. |
| GET | `/api/jobs/{id}` | Report, cues, evidence/preview. |
| POST | `/api/catalog/import` | `{}`; returns imported count and `enqueued: 0`. |
| POST | `/api/catalog/queue` | `{ids: [explicit target IDs]}`; 1–500 eligible targets. |
| POST | `/api/jobs` | `{media_path, subtitle_path, language: "en", audio_index?: integer}`. |
| POST | `/api/jobs/{id}/retry` | `{audio_index?: integer}`. |
| POST | `/api/jobs/{id}/preview` | `{anchors: [{cue_index: 0, time: 12.5}]}`. |
| POST | `/api/jobs/{id}/apply` | `{}`; successful manual preview required. |
| POST | `/api/jobs/{id}/rollback` | `{}`; guarded original restoration. |

POST requests require `Content-Type: application/json` and `X-BBS-Client: 1`. Body limit is 1 MiB. HTTP access logging is suppressed to keep private paths/keys out of logs; operational failures appear in job reports. The standard-library HTTP server belongs behind a trusted proxy with TLS and connection/request limits, not directly on the public Internet.

## Image verification

The Dockerfile installs FFmpeg and runs unprivileged without GPU devices. An optional ASR build installs the pinned top-level faster-whisper dependency. Image tag/registry, a complete dependency lock, non-amd64 support, production limits, retention, and deployment resources remain open owner decisions. CI builds and health-checks the base image; verify the actual CI result before claiming image validation. See benchmark notes for completed Linux source/image checks.

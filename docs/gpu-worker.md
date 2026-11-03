# Desktop GPU worker and coordinator `remote-gpu` mode

The coordinator queues jobs, prepares audio, validates results, renders proposals and alone publishes subtitles. The desktop worker connects outward, claims one job at a time, computes cue timings on its GPU and returns them. The method and its evidence are in [benchmark-sequence-mapper-2026-11-03.md](benchmark-sequence-mapper-2026-11-03.md).

## Coordinator settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `BBS_BACKEND` | `disabled` | Set `remote-gpu` to hand queued jobs to desktop workers. |
| `BBS_WORKER_TOKENS` | `{}` | JSON object `{worker name: sha256 hex of its token}`. Required for `remote-gpu`. Tokens themselves are never stored. |
| `BBS_LEASE_SECONDS` | `300` | Lease length (30–3600). Workers heartbeat every third of it. |
| `BBS_MAX_ATTEMPTS` | `3` | Expired leases requeue a job until this many attempts, then it fails and needs an explicit retry. |
| `BBS_AUTO_APPLY` | `false` | Opt-in automatic publication of confident proposals. |
| `BBS_AUTO_APPLY_MIN_COVERAGE` | `0.8` | Minimum share of spoken cues measured directly for automatic apply. |

To create a worker token:

```sh
python -c "import secrets,hashlib;t=secrets.token_urlsafe(32);print('token:',t);print('sha256:',hashlib.sha256(t.encode()).hexdigest())"
```

Give the token to the desktop. Put the sha256 into `BBS_WORKER_TOKENS`. Remove the entry to revoke the worker.

## Flow and states

`queued` → `running` (the coordinator extracts the selected audio track to a 16 kHz mono WAV under `/data/artifacts`) → `awaiting_worker` → `worker_running` (leased) → `awaiting_apply`, `applied` (automatic), `unchanged` or `failed`. `cancelled` is reachable from any queued or worker state.

- **Results.** A result is accepted only with the current lease, matching subtitle and audio hashes, exactly one finite, increasing, in-bounds timing per cue, and an unchanged source subtitle. The coordinator renders the original document with the new times, keeping wording, formatting and BOM, and stores the proposal with its hash. The audio artifact is deleted once a result is accepted, or when the job fails or is cancelled.
- **Idempotency.** Repeating the same `result_id` returns the earlier acknowledgement. A different result for a completed attempt, a late result after cancellation, and a result on an expired lease are all rejected with 409.
- **Workers cannot publish.** Their token only reaches `/worker/v1/*`. Apply and rollback stay on the operator API and the web UI.

## Automatic apply policy

Agreed with the owner on 2026-11-03. Automatic apply is **off by default**. When `BBS_AUTO_APPLY=true`, a proposal is applied without review only if:

- at least `BBS_AUTO_APPLY_MIN_COVERAGE` of spoken cues were measured directly from speech, and
- it introduces no start inversions and no new overlaps.

Everything else waits in `awaiting_apply`, with the reasons in its report. Automatic apply uses the same backup, hash recheck, atomic replace and rollback path as manual Apply.

Recommendation: start with automatic apply off. Review a few proposals from each new series or release first. The method has been evaluated on one series, one release and English only.

## Worker protocol (v1)

All requests send `Authorization: Bearer <token>`. Every body except audio is JSON. The worker sends its lease token in each body, or in the `X-BBS-Lease` header for audio.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/worker/v1/claim` | `{protocol: 1}`. Returns 204 when idle, otherwise the job ID, lease token, audio description with sha256, cue list and recipe. Never returns a path. |
| GET | `/worker/v1/jobs/{id}/audio` | Streams the job's WAV artifact. The worker verifies size and sha256. |
| POST | `/worker/v1/jobs/{id}/heartbeat` | Renews the lease. 409 means stop: the lease was revoked or expired. |
| POST | `/worker/v1/jobs/{id}/result` | Per-cue timings, engine description and diagnostics. Body limit 16 MiB. |
| POST | `/worker/v1/jobs/{id}/fail` | Reports a worker-side failure. The job fails and needs an explicit retry. |

**Connectivity (owner decision 2026-11-03): private network only.** The desktop reaches the coordinator over the home LAN or a private overlay such as Tailscale or WireGuard. The worker endpoints are never published on a public ingress. The operator API (`/api/*` and the web UI, where proposals are reviewed and applied) stays behind Authentik.

## Running the worker on Windows

Requirements: an AMD GPU with about 10 GB VRAM and Python 3.12. The ROCm build of torch is pinned in `worker/requirements-gpu.txt`, the environment that produced the benchmarks.

From a checkout of this repository, install for the current user (no admin rights needed):

```powershell
.\worker\windows\install.ps1 -Coordinator https://bazarr-sync-worker.<tailnet>.ts.net -ReviewUrl https://bazarr-sync.example
```

The installer puts everything under `%LOCALAPPDATA%\BazarrSyncWorker`:

- `venv\`: the Python environment.
- `models\`: about 5.6 GB, downloaded on the first job.
- `alass\`, `config.json`, `logs\` and `scratch\`.

It asks for the worker token once and keeps it only in the user environment variable `BBS_WORKER_TOKEN`. It turns on start at login and launches the tray app.

To reuse an existing setup, pass `-ExistingVenv` and `-ExistingModels` (both are moved into place) and `-ExistingAlass` (copied). A fresh machine downloads AMD's nightly ROCm wheels. If those have been withdrawn upstream, install a current ROCm torch yourself and pass that environment with `-ExistingVenv`.

The **tray app** (`bazarr-sync-tray`, or `pythonw -m bazarr_sync.worker.tray`) sits in the notification area:

- **Icon colour** shows the state: green idle, blue syncing, grey paused, orange coordinator unreachable, red error. The tooltip says what it is doing.
- **Menu:** open the review page, pause/resume, start at login (a per-user Run registry entry), open logs, quit.
- **Notifications:** a Windows notification appears when a proposal is ready for review.
- **One instance only.** A second launch exits immediately, so two workers never compete for the GPU.

Behaviour:

- **Models.** They load on the first job, about 15 s and 6 GB VRAM. After 10 idle minutes they are unloaded so the GPU is free for other use.
- **GPU problems.** If the models cannot load, the job is not marked failed. Its lease expires and the coordinator requeues it.
- **Audio and scratch.** Audio stays in a per-job scratch folder, deleted when the job ends. Leftover `job-*` folders are removed at startup.
- **Leases.** A lost lease stops the job without sending a result.
- **Connection.** If the coordinator is unreachable, the worker keeps polling.
- **Quitting mid-job.** The lease expires and the job is requeued.

For a console instead of the tray, run `python -m bazarr_sync.worker`, adding `--once` to process at most one job. Flags override `config.json`.

To remove it, run `.\worker\windows\uninstall.ps1`. Add `-RemoveFiles` to delete the install folder and `-RemoveToken` to clear the token.

# Bazarr Better Sync

An experimental local subtitle alignment companion for upstream Bazarr, designed for a containerized Kubernetes environment without maintaining a Bazarr fork.

The initial scope is queued, CPU-based English SRT/ASS/SSA alignment, shared media mounts, and a web UI for reports, retries, audio-track selection, and manual timestamp anchors. GPU backends and Turkish alignment are future work.

Browser access is intended to sit behind Authentik. Bazarr connects to the internal API; network restrictions are managed by the deployment owner.

## Status

The initial application provides metadata-only catalog import, selected-item processing, a durable queue, job reports, audio selection, manual timing previews, original backups, and guarded rollback. SRT, ASS, and SSA timestamp editing preserves authored content. Imports leave targets unselected and enqueue no analysis.

**Automatic model-based replacement is disabled.** The CPU speech-anchor candidate produces reviewable evidence; quality and numeric release criteria have not been established. It currently fits global offset/drift and abstains on insufficient evidence or inconsistent cuts. Manual corrections require preview and explicit Apply. Production Bazarr and Home-Infra configuration is not changed by this repository.

Read [SPEC.md](SPEC.md), [engineering decisions](docs/engineering-decisions.md), [operating instructions](docs/operations.md), and the [CPU benchmark report](docs/benchmark-2026-10-29.md). Deployment manifests and Helm charts remain owner-maintained. No software license or image registry has been selected.

## Run from source

Python 3.11+ and FFmpeg/ffprobe on PATH are required. The base application has no third-party Python runtime dependencies.

```sh
python -m pip install .
export BBS_DATA_DIR=/path/to/persistent/state
export BBS_MEDIA_ROOTS='["/media"]'
export BBS_PATH_MAPPINGS='{"/movies":"/media/movies","/tv":"/media/tv"}'
export BBS_BAZARR_URL=http://bazarr:6767
# Provide BBS_BAZARR_API_KEY through your private environment/secret manager.
python -m bazarr_sync
```

Visit port 8080. Import, filter by series/season/movie, select explicit eligible targets, and analyse the selection. In default manual mode, the worker probes audio metadata and shows cues without downloading a model. Enter intended cue-start anchors as `cue_index, seconds`, review the preview, and choose Apply if correct. Configure actual mount mappings; example paths above are illustrative.

## Container

```sh
docker build -t bazarr-better-sync:dev .
# Optional local CPU speech inference dependencies:
docker build --build-arg INSTALL_ASR=1 -t bazarr-better-sync:asr-dev .
```

The image runs as UID/GID 10001 on port 8080. Mount writable persistent `/data` and explicitly configured media mounts. Start with read-only media for discovery/analysis; manual Apply requires subtitle-directory write access. Supply the same `BBS_*` settings. Browser ingress belongs behind owner-managed Authentik; restrict direct access to this unauthenticated service.

## Optional speech candidate

Install `.[asr]` or build the ASR image, set `BBS_BACKEND=faster-whisper`, and select `BBS_MODEL` and `BBS_THREADS`. Inference runs locally on CPU INT8. Named models download on first use. Immutable local model directories permit content-keyed transcript-cache reuse; named remote aliases deliberately do not reuse transcripts across jobs. Model choice remains experimental.

## Bazarr download hook

Copy `src/bazarr_sync/submit.py` into Bazarr's mounted configuration as a standalone script. It needs Python standard library only. Use the video/subtitle substitutions for your installed Bazarr version, as described in [operations](docs/operations.md). The client submits a durable job and returns promptly:

```sh
python /config/bazarr-sync-submit.py --url http://companion:8080 --video "/tv/example/video.mkv" --subtitle "/tv/example/video.en.srt" --language en --timeout 10
```

Replace those illustrative paths with Bazarr's command substitutions. Verify on scratch files first. This repository does not install the hook or replace Bazarr's manual Sync button. Acceptance is not completed alignment.

## Verification

```sh
python -m unittest discover -s tests -v
python -m bazarr_sync.benchmark --synthetic --output /tmp/synthetic-report.json
```

Tests cover import/selection boundaries, durable deduplication, interrupted work, prompt acknowledgements during inference, path and origin checks, audio ambiguity, subtitle preservation, held-out fitting, manual previews, backups, rollback, stale files, and locking. Synthetic results validate mechanics, not speech accuracy or superiority to ffsubsync. The real-media benchmark found that all three speech-model candidates abstained; see the report for measured cost and remaining quality evidence.

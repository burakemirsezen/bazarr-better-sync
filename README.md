# Bazarr Better Sync

A planned subtitle alignment companion for upstream Bazarr, designed for a containerized Kubernetes environment without maintaining a Bazarr fork.

The initial scope is queued, CPU-based English SRT/ASS/SSA alignment, shared media mounts, and a web UI for reports, retries, audio-track selection, and manual timestamp anchors. GPU backends and Turkish alignment are future work.

Browser access is intended to sit behind Authentik. Bazarr connects to the internal API; network restrictions are managed by the deployment owner.

## Status

Specification only. There is no application or container image yet.

Read [SPEC.md](SPEC.md) for confirmed requirements, proposed engineering behavior, evaluation expectations, and decisions that must be settled before implementation. Deployment manifests and Helm charts are outside this repository's requested deliverables.

# Bazarr Better Sync — Product and Engineering Specification

Status: confirmed requirements baseline; routine engineering decisions are delegated within the constraints below.

Date: 2026-10-29

Repository: https://github.com/burakemirsezen/bazarr-better-sync

## 1. Purpose

Build a subtitle alignment companion for upstream Bazarr that improves the reliability of English subtitle timing. The primary use case is high-quality English subtitles downloaded from OpenSubtitles for media with English audio. Turkish subtitles are a secondary, future concern.

The system should use the existing subtitles as the authored content and derive reliable timing evidence from the media. It must be evaluated against actual problematic files before claiming an improvement over Bazarr's existing ffsubsync workflow.

This repository starts with a specification. Creating an application, container image, or deployment is subsequent work, not an outcome already delivered by this document.

## 2. Decisions confirmed with the owner

| Area | Confirmed decision |
| --- | --- |
| Repository | Public repository named `burakemirsezen/bazarr-better-sync`. |
| Bazarr | Keep using upstream Bazarr; no separately maintained fork. |
| Integration | A small custom post-processing client submits queued jobs and returns promptly. |
| Job interaction | Progress, results, reports, and retries are available in the companion's web UI. |
| Existing library | Import an inventory through Bazarr's API; analyse and repair only the movies, episodes, or seasons explicitly selected by the user. Import alone must not enqueue alignment jobs or change subtitles. |
| First-version languages | English subtitle alignment against English audio. Turkish is deferred. |
| First-version formats | SRT, ASS, and SSA. |
| Uncertain automatic results | Keep the original subtitle; expose the reason and correction controls in the UI. |
| Manual controls | Job reports, retry, audio-track selection, and manual timing anchors entered as timestamps. |
| Media preview | An embedded video player is outside the selected first-version UI scope. |
| Compute | CPU operation first, with the code structured to allow GPU inference backends later. No GPU requirement for the first version. |
| Inference locality | Process media and subtitle content locally. Model downloads are allowed; external AI processing is not part of the approved design. |
| Runtime environment | Containerized service intended to run in Kubernetes. |
| Runtime complexity | One application instance with persistent state, without a required separately operated database or queue service. |
| Deployment deliverables | Container image and operating documentation. The owner manages Kubernetes deployment separately; no Helm chart or Kubernetes manifests are requested. |
| File access | Shared media storage mounted into the service, with configurable mappings from Bazarr paths. Media upload/download is not required. |
| Browser authentication | Authentik in front of the application, configured by the owner. No application-managed login or accounts. |
| Bazarr API access | Direct internal Kubernetes connection with no application authentication; access restrictions belong to the owner's deployment. |
| Validated automatic results | Apply validated corrections automatically, preserving originals and providing rollback. Uncertain results remain unchanged. |
| Manual corrections | Show a preview and require an explicit Apply action for manually anchored corrections. |
| Engineering authority | Agents may choose and document routine implementation details. Scope changes, new operational dependencies, and deployment-requirement changes need owner approval. |
| Development order | Benchmark alignment candidates and establish evidence of quality before building the full workflow around the chosen approach. |

Later decisions must be recorded explicitly. Delegated engineering choices must include their reasoning and relevant evidence. Remaining owner decisions must be asked rather than silently assumed.

## 3. Scope and boundaries

### First-version product scope

- Accept work submitted after a Bazarr subtitle download through its existing custom post-processing facility.
- Import the existing library into a browsable catalog from Bazarr's API and permit selected movies, episodes, or seasons to be submitted for analysis and repair.
- Align English SRT, ASS, and SSA subtitles against a selected English audio track.
- Correct supported timing problems while preserving the authored subtitle content and presentation.
- Show queued and running work, completion outcomes, uncertain results, and failures.
- Allow a user to select the audio reference, retry a job, and supply manual timing anchors.
- Work on CPUs and expose a clean boundary for future GPU-capable inference implementations.
- Read media and update subtitles through configured shared-storage paths.

### Deferred or unselected scope

- Turkish or other translated-subtitle alignment.
- GPU execution, including Intel iGPU support.
- Embedded browser video playback or media streaming.
- Subtitle generation, translation, or rewriting as a replacement for the downloaded subtitle.
- Replacing Bazarr's built-in manual Sync button.
- A Bazarr fork, custom Bazarr distribution, or mandatory upstream code changes.
- Media uploads and corrected-file downloads as the normal integration mechanism.
- Helm charts, Kubernetes manifests, ingress deployment, Authentik configuration changes, or live-cluster changes.

Catalog import and processing selected existing items are first-version requirements. Background assessment of the whole library, scheduled rescans, automatic subtitle re-downloads, and direct media-player refresh integrations have not been approved. Import must not imply authorization to process or repair the whole library.

## 4. Bazarr integration contract

The post-processing client receives the video path, subtitle path, and available language/context metadata from Bazarr's existing command substitutions. Exact command syntax and API routes will be specified during implementation.

The intended sequence is:

1. Bazarr downloads and saves a subtitle normally.
2. Its configured post-processing command invokes the submission client.
3. The client submits the job using the internal service address.
4. The service acknowledges acceptance and returns a job identifier after the job has been recorded for processing.
5. The client returns to Bazarr without waiting for transcription or alignment.
6. The worker processes the mounted files and records the result.
7. The user inspects the result or performs a retry/manual correction in the web UI when necessary.

Bazarr's built-in automatic sync should be disabled when this service is responsible for automatic alignment. Post-processing score thresholds must be configured so that high-scoring subtitles are not inadvertently excluded; Bazarr's release-match score is not evidence that subtitle timing is correct.

The documentation must explain that this hook applies to the supported download flow. It does not replace the built-in manual Sync button or automatically enqueue every subtitle already present in the library.

Because work is asynchronous, a player may initially see the original subtitle. Job completion must be visible in the companion UI. Whether a particular media server requires a refresh to observe later subtitle changes must be verified separately; the initial integration must not promise refresh behavior it does not implement.

The submission client needs a bounded request timeout and actionable output for submission failures. A successful submission means the job was accepted, not that alignment has succeeded. Retry and duplicate-submission semantics must be specified before implementation.

### Existing-library discovery and selection

Use read requests to Bazarr's API to populate the companion's catalog. Do not require a Bazarr database mount or couple the product to direct database access. Verify the API contract against the installed Bazarr version during implementation.

The API currently exposes movie and series listings, episodes for selected series, and subtitle metadata including language, path, and embedded-track information. Import should record source identifiers and map the returned media/subtitle paths to the configured shared mounts. The implementation must handle paging and per-series episode retrieval without assuming one response contains the whole library.

Discovery and analysis are separate operations:

1. The owner configures the Bazarr URL, its API key, and path mappings.
2. An import reads library/subtitle metadata and builds or updates the local catalog. Import does not run transcription, decode entire media files to assess timing, enqueue alignment jobs, or modify subtitles.
3. Imported items are marked as unassessed, not presumed correctly synced or out of sync.
4. The owner selects movies, episodes, or seasons and starts processing. The UI must show the selected subtitle targets and item count so the scope is clear before submission.
5. Only selected eligible targets enter the alignment queue. A selection must not silently expand to the whole series or library.
6. Validated automatic corrections follow the approved backup/rollback policy; uncertain results remain unchanged and available for review.

V1 alignment eligibility remains English SRT/ASS/SSA on accessible shared storage. Non-English subtitles may be represented in the catalog but must not be queued for an unsupported alignment workflow. Embedded subtitle tracks must be identified separately; discovery does not authorize rewriting media containers or automatically extracting/replacing embedded tracks. Missing, stale, unsupported, or unmappable entries must be reported without deleting or modifying the original files.

Repeated imports must reconcile source entries without generating duplicate alignment work. A catalog refresh is not an instruction to rerun prior jobs. Automatic refresh scheduling is not selected; provide an explicit import/refresh action initially.

The companion uses Bazarr's API for discovery, not to invoke Bazarr's existing sync, change language profiles, or trigger bulk download/repair actions. API credentials must stay out of public repository content and diagnostic logs.

## 5. Logical architecture

```mermaid
flowchart LR
    B[Bazarr post-processing client] -->|Internal job submission| A[Application API]
    D[Bazarr library API] -->|Read-only metadata import| L[Library catalog]
    U[Browser] --> I[Owner-managed ingress and Authentik]
    I --> W[Web UI and application API]
    W --> J[Job state and queue]
    W -->|Explicit selection| L
    L -->|Selected targets only| J
    A --> J
    J --> P[Alignment worker]
    P --> M[Shared media and subtitle storage]
    P --> C[Inference cache and job reports]
```

These are logical responsibilities within the approved single application instance. Persistent job state is required; a separately operated database or queue must not become a mandatory dependency without owner approval. Agents may select internal process topology, storage technology, queue implementation, UI framework, and backend language, documenting the tradeoffs.

### Required responsibilities

- **Submission client:** pass input references and supported metadata, handle acknowledgement and connection failures, and return promptly to Bazarr.
- **API:** validate submissions, expose job state and reports, and accept retry and manual-anchor actions.
- **Library connector/catalog:** read Bazarr's library metadata, reconcile catalog entries, expose explicit item selection, and keep discovery separate from analysis and repair.
- **Job coordination:** control concurrency, identify repeated work, and report interruption/recovery outcomes.
- **Worker:** inspect input files, select or accept an audio reference, obtain timing evidence, align, validate, and publish a supported result.
- **Web UI:** present enough evidence for a user to understand an outcome and correct it without editing subtitle files manually.
- **Inference backend interface:** isolate model loading, device selection, transcription/alignment output, and inference-specific configuration from job orchestration and subtitle editing.

## 6. Alignment approach to evaluate

The following is an engineering proposal for evaluation. Agents may choose a model/library based on benchmark evidence within the approved CPU/local-processing constraints:

1. Inspect the media and enumerate audio tracks. Record the chosen track in the job report. Selection rules for ambiguous, missing, dubbed, or commentary-track metadata must be decided explicitly; the user must be able to override the choice.
2. Parse subtitle cues without discarding text, formatting, styles, or unrelated metadata.
3. Obtain timestamped English speech evidence from the selected audio track. Reuse valid cached evidence when possible.
4. Match distinctive subtitle passages to the recognized dialogue in sequence, tolerating recognition errors, punctuation differences, condensed wording, and missing passages.
5. Infer timing anchors and test supported correction models: constant offset, global drift, and changes between sections when the evidence supports them.
6. Derive cue timings while preserving sensible display durations and the source's intended presentation. Word timestamps are evidence; they are not automatically a caption's exact display boundaries.
7. Validate the proposed result across the media, including regions not used to fit the correction. Produce a report explaining support, uncertainty, and changes.
8. Install a successful result according to an explicitly defined file-update policy. If evidence is insufficient, preserve the existing subtitle and expose the job for review.

WhisperX, CPU-capable Whisper implementations, ffsubsync, and alass are candidates to assess. No candidate is mandated by this spec. Agents must document model/backend selection, precision, licensing constraints, and measured quality/resource tradeoffs rather than claim a performance result before benchmarking.

The system must not assume that a high-quality subtitle is a verbatim transcript, that every line corresponds to speech, or that every subtitle cue has a one-to-one transcript match. Repeated phrases, songs, overlapping speakers, sound-effect captions, sparse/forced subtitles, and different video edits belong in evaluation.

A subtitle cannot restore dialogue absent from the video. Different edits can require a partial alignment or an uncertain outcome rather than a confidently incorrect rewrite.

## 7. Subtitle preservation and file handling

The product goal is timing correction, not content regeneration. English subtitle wording, cue order, styles, speaker information, and non-dialogue content should survive processing.

SRT, ASS, and SSA must have separate preservation checks. ASS/SSA styles, event fields, override tags, and intentional overlaps must not be flattened into SRT or silently normalized. Handling of embedded time-dependent constructs such as karaoke and animation needs an explicit implementation decision. Unsupported constructs must be reported rather than silently corrupted.

Shared-storage integration must account for:

- Mapping Bazarr's submitted paths to the worker's mount paths, with explicit configured roots.
- Read access to media and appropriate write access to subtitle destinations.
- Subtitle encodings, filenames, and formats.
- The subtitle being replaced or upgraded while a queued job is waiting or running.
- Concurrent jobs targeting the same subtitle.
- Restarts, cancellation/interruption, and write failures.

Before an automatic write, the worker must ensure it is still modifying the intended input version and avoid publishing a partial file. Validated automatic corrections may replace the active subtitle only after preserving its original and recording a usable rollback reference. The UI must provide rollback; rollback must also respect later file changes rather than overwrite a newer subtitle blindly.

Agents may choose and document locking, version checks, replacement, and backup/rollback mechanisms. Backup retention/storage policy remains an owner decision; automatic expiry or deletion must not silently remove the only recovery copy.

## 8. Web UI and manual timing anchors

The UI must expose the imported library catalog and explicit target selection, a job list, job detail/report view, retry control, audio-track selection, timestamp-based anchor entry, and rollback. It does not require a browser video player.

A job report should distinguish an unchanged/already-aligned result, an applied correction, an uncertain result needing review, and an operational failure. Reports should identify the inputs, audio track, processing stage, proposed/applied timing changes, supporting matches, and unresolved regions. Exact status names and the confidence presentation are not selected yet; an uncalibrated score must not be presented as a guaranteed probability of correctness.

### Manual interaction

1. Open an uncertain job and review the report.
2. Select or confirm the intended audio track.
3. Select a subtitle cue and enter the time at which it should occur, using the owner's normal media player to find that time.
4. Add further anchors when needed to describe drift or separate timing sections.
5. Run a constrained retry and inspect its proposed changes and validation report.

The anchor UI must make the timestamp's meaning explicit, such as the selected cue's intended start time. It must validate anchor order, media bounds, and contradictory entries.

One anchor can support an offset correction; two or more can constrain drift. Cuts may require anchors in separate sections. An anchor does not prove that every intervening subtitle matches the audio.

Manually derived output must be previewed and installed only after the user selects Apply. Manual input must not bypass validation, original preservation, or file consistency checks. Agents may select and document anchor interpolation/section rules and the preview representation.

## 9. CPU first; GPU path later

The first implementation and container image must operate without a GPU. No CUDA dependency or GPU device must be mandatory to start the service or run a job. Media/subtitle inference must execute locally; downloading model artifacts is allowed, but sending library content to external AI services is outside the approved design.

Inference configuration and execution must be isolated behind a backend boundary so that future GPU support does not require rewriting the queue, API, subtitle parsers, or manual-anchor UI. Backend/device/model identity must be included in relevant cache metadata and diagnostic reports.

An Intel iGPU is available in the intended environment, but its model and compatibility are not established. Intel acceleration is a future capability, not a promised first-version backend.

CPU throughput, memory use, and acceptable turnaround time must be measured on representative hardware and media. The specification deliberately sets no invented core count, RAM limit, concurrency default, or minutes-per-episode target.

Caching should avoid repeating expensive speech analysis for the same valid media/audio input. Agents may choose and document cache identity and invalidation; a filename alone is insufficient evidence that content is unchanged. Storage budgets and retention remain owner decisions.

## 10. Container and Kubernetes operating contract

The implementation must provide a reproducible container build for one application instance with persistent state, and document how to run the resulting image in the owner's Kubernetes environment. Accepted jobs must survive application restarts, with interrupted work recovered or surfaced according to a documented policy. Deployment resources are maintained elsewhere by the owner.

The operating documentation must describe:

- Supported image architectures and tags once chosen.
- Start command and process modes once the runtime topology is chosen.
- Network listener and health/readiness interfaces.
- Shared media mounts, path mappings, and filesystem permissions.
- Persistent job data, inference/model cache, temporary space, and reports/backups as applicable.
- Configuration inputs and their meanings, including CPU concurrency/resource controls.
- Startup/model preparation, job interruption/recovery, and upgrade compatibility.
- Logs and diagnostic data needed to investigate failures.
- The trusted-ingress and internal-API deployment boundary described below.

Container registry, target architectures, numeric resource budgets, and retention settings remain owner decisions. Agents may choose and document the publishing/build implementation, base image, model provisioning strategy, listener/health interfaces, and container-side paths within the agreed operating contract. New required external services or changed deployment requirements need owner approval. Building an image must not imply deployment to the owner's cluster.

## 11. Access boundary: Authentik and internal API

The application will not implement its own accounts, password storage, interactive login, or API authentication. This replaces the earlier built-in-authentication request.

- Browser access goes through owner-managed ingress protected by Authentik.
- The Bazarr submission client uses the internal Kubernetes service directly, without interactive login or application credentials.
- The separate outbound connection from the companion to Bazarr uses Bazarr's API key for library discovery. This does not add authentication to the companion's inbound API.
- The owner manages restrictions on direct service access so that the external route cannot bypass Authentik and untrusted workloads cannot reach the unauthenticated API.
- The application must document that direct access grants its supported operations; a private service address alone is not a user-authentication mechanism.
- The UI and state-changing API actions still require ordinary request validation. The implementation must resolve browser request-origin/CSRF behavior for the reverse-proxy deployment.

Forwarded identity headers, per-user auditing, and application roles are not required or selected. If added later, they need an explicit trusted-proxy contract; client-supplied identity must not silently become authorization.

No Authentik, ingress, network-policy, or cluster change is part of the current repository/specification task.

## 12. Evaluation and acceptance plan

The intended benefit is fewer incorrect alignments and fewer harmful automatic changes. Success must be demonstrated, not inferred from a model's transcription accuracy or a tool's exit status.

### Benchmark-first implementation gate

Agents should start with a reproducible alignment evaluation harness and candidate experiments before committing to a model/backend or building the full product workflow. Compare existing tools and timestamped speech approaches on the same inputs, recording settings, timings, resource consumption, preservation checks, and results. Use the evidence to select and document the initial implementation approach.

Owner-selected real failures and already-correct examples are needed to establish practical improvement. Synthetic offsets/drift can help develop the harness but cannot by themselves demonstrate that the product fixes the owner's real failures. If representative inputs are unavailable, agents must identify that evaluation gap and keep any quality conclusion provisional.

Create a representative, owner-approved evaluation set covering:

- Subtitles already correctly aligned.
- Constant delay and progressive drift.
- Recaps/intros, missing or additional scenes, and abrupt timing changes.
- Condensed/paraphrased English subtitles and recognition mistakes.
- Repeated dialogue, silence/music, overlapping speech, and non-dialogue cues.
- All three requested formats, including ASS/SSA preservation cases.
- Incorrect audio-track selection and a corrected manual retry.
- Manual-anchor workflows.

Compare the companion with Bazarr/ffsubsync under recorded settings. Evaluate independent timing references distributed across the media; do not validate only on the anchors used to fit a correction.

Measure cue timing error, alignment coverage, harmful changes to already-correct subtitles, uncertain/rejected cases, runtime, and peak memory. Agree on the benchmark set and numeric pass criteria with the owner before using the results as a release gate.

Operational verification must cover metadata-only catalog import, repeat imports, English/format eligibility, selected-item boundaries, submission acknowledgement, duplicate requests, worker interruption, stale inputs, concurrent updates, path mappings, inaccessible storage, malformed files, replacement/rollback, and explicit Apply for manual corrections. Verify that importing or refreshing a library creates no alignment jobs and changes no media/subtitle files. Container checks must demonstrate CPU operation without GPU devices and the documented health/configuration interfaces.

No live-media corpus, credentials, or private infrastructure configuration should be committed as test fixtures to this public repository.

## 13. Remaining owner decisions and delegated engineering choices

| Decision | Current status |
| --- | --- |
| Backend language, API framework, and UI framework | Delegated to agents; record reasoning. |
| ASR/word-alignment engine, model, precision, and optional fast alignment | Delegated to agents after comparative benchmarks; CPU/local inference and format-preservation requirements remain binding. |
| Queue, internal persistence, process topology, restart/retry policy | Delegated within one app instance with persistent state and no required external database/queue. Acknowledge accepted work durably and document recovery. |
| Ambiguous audio-track selection rules | Agents may design and test the policy; manual selection is required and ambiguity must not be hidden. |
| Sparse/forced subtitles and advanced ASS time-dependent tags | Agents must specify/test behavior; no silent corruption or format conversion. Scope expansion requires owner approval. |
| Applying successful results | Confirmed: automatic validated corrections with originals preserved and rollback; uncertain results stay unchanged. Mechanisms are delegated. |
| Manual-anchor model and preview | Interpolation and presentation are delegated. Preview plus explicit Apply is required. |
| Locality/model provisioning | Local inference is confirmed; model downloads allowed. Provisioning implementation is delegated. |
| Confidence thresholds and benchmark release criteria | Agents propose from evidence; owner agrees numeric release criteria. |
| Benchmark media and intended-node CPU/RAM/turnaround budget | Owner input still needed; measure actual performance and keep unsupported quality claims provisional. |
| Target container architectures and registry | Owner decision remains open. Build/publishing mechanics may be proposed by agents. |
| Backup/cache/report retention and storage/temporary-space limits | Owner decision remains open; agents may propose settings and implement configurable controls. |
| Existing library | Confirmed: Bazarr API catalog import; only explicitly selected movies/episodes/seasons are analysed or repaired. No background whole-library assessment. |
| Scheduled refresh/rescan and direct media-server refresh | Scope remains unapproved. |
| Software/documentation license | Not selected; do not add a license by assumption. |

Agents may research and implement delegated technical choices without repeatedly asking the owner, provided they document the choices and stay within approved scope, operational dependencies, and deployment requirements. Ask before changing those boundaries or deciding the owner-owned items above.

## 14. References and current integration observations

Reviewed on 2026-10-29. Upstream branch links can change; verify behavior against the installed Bazarr version during implementation.

- [Bazarr subtitle settings and custom post-processing](https://wiki.bazarr.media/Additional-Configuration/Settings/Subtitles/) documents the hook and command substitutions.
- [Bazarr post-processing implementation](https://github.com/morpheus65535/bazarr/blob/master/bazarr/subtitles/post_processing.py) waits for the command via `communicate()` with no explicit timeout in the inspected function. The queued submission client must therefore return promptly and implement its own request timeout. In the inspected version, output/error logging is not a rich job-result integration.
- [Bazarr subtitle processing flow](https://github.com/morpheus65535/bazarr/blob/master/bazarr/subtitles/processing.py) applies existing sync before custom post-processing and performs subsequent notifications. Later companion completion does not automatically reuse that notification sequence.
- [Bazarr sync integration](https://github.com/morpheus65535/bazarr/blob/master/bazarr/subtitles/tools/subsyncer.py) calls ffsubsync; FFmpeg is used for media handling.
- [Bazarr movies API](https://github.com/morpheus65535/bazarr/blob/master/bazarr/api/movies/movies.py), [series API](https://github.com/morpheus65535/bazarr/blob/master/bazarr/api/series/series.py), and [episodes API](https://github.com/morpheus65535/bazarr/blob/master/bazarr/api/episodes/episodes.py) expose library listings. The inspected episode endpoint requires series or episode IDs; pagination and library traversal must be implemented deliberately.
- [Bazarr API processing and authentication](https://github.com/morpheus65535/bazarr/blob/master/bazarr/api/utils.py) attaches subtitle metadata and accepts the Bazarr API key through `X-API-KEY`. [Subtitle response fields](https://github.com/morpheus65535/bazarr/blob/master/bazarr/api/swaggerui.py) include language, path, and embedded-track identity. Verify these contracts against the installed version.
- [ffsubsync](https://github.com/smacke/ffsubsync) provides the existing alignment baseline and subtitle-reference support.
- [alass](https://github.com/kaegi/alass) supports offsets, framerate differences, and splits; it is an evaluation candidate.
- [WhisperX](https://github.com/m-bain/whisperX) provides timestamped speech recognition/word alignment and CPU execution; it is a candidate, not a selected dependency.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) documents its inference options. Its documented GPU route uses NVIDIA libraries; Intel iGPU support must not be assumed from the word GPU.
- [Authentik forward authentication](https://docs.goauthentik.io/add-secure-apps/providers/proxy/forward_auth) explains the external browser-authentication pattern. The selected Bazarr API route is direct/internal and does not require Authentik machine credentials.

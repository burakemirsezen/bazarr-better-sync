# CPU benchmark, 2026-10-29

This is an engineering experiment, not a release-quality gate. The prototype has
not demonstrated better timing than ffsubsync. Automatic subtitle publication
remains disabled. The selected existing subtitle has suspected timing problems;
its timestamps are a provisional reference rather than human ground truth.

## Environment and isolation

The intended Kubernetes worker provides four virtual CPUs and about 11.7 GiB
memory. The guest CPU identifies itself as `QEMU Virtual CPU version 2.5+`, so the
physical CPU model is not established. The disposable benchmark pod was limited
to two CPUs and 4 GiB RAM, used a read-only media PVC and an `emptyDir` scratch
volume, had no service-account token and no production configuration mount.
No production deployment, Bazarr setting, media file or library subtitle was
changed. The selected subtitle's SHA-256 was checked before and after processing.

The benchmark image was `python:3.11-slim-bookworm`, resolved to
`docker.io/library/python@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd`.
The installed Bazarr version is 1.5.6 and its bundled ffsubsync version is 0.4.27.
Observed Python dependency versions are recorded in
[benchmark-environment.txt](benchmark-environment.txt). That file is an
environment inventory, not a portable dependency resolver lock: on Linux,
ffsubsync declares `webrtcvad`, while this experiment used the compatible
`webrtcvad-wheels` distribution to avoid compiling extensions in the scratch pod.

Audio was decoded using the imageio-ffmpeg 0.7.0 supplied static FFmpeg 7.0.2.
Inference used faster-whisper 1.2.1 / CTranslate2 4.8.4, CPU `int8`, two threads,
one worker, English, beam size 5, word timestamps, VAD, and
`condition_on_previous_text=False`. Segments with `no_speech_prob > 0.6` and
nonpositive-duration words were excluded, matching the application adapter.

Public model repositories and their downloaded snapshot revisions:

| Repository | Revision |
| --- | --- |
| `Systran/faster-whisper-tiny.en` | `0d3d19a32d3338f10357c0889762bd8d64bbdeba` |
| `Systran/faster-whisper-base.en` | `3d3d5dee26484f91867d81cb899cfcf72b96be6c` |
| `Systran/faster-whisper-small.en` | `d1d751a5f8271d482d14ca55d9e2deeebbae577f` |

## Corpus and method

One owner-selected English episode supplied a 480-second window starting at
120 seconds, with 154 fully contained external SRT cues. The single explicitly
tagged English audio stream was decoded to mono 16 kHz WAV. The source subtitle
and media stayed on the read-only mount; all altered subtitles and transcripts
were written to scratch. Private filenames, dialogue, source fingerprints and
corpus files are excluded from this repository.

Four input variants were evaluated: unchanged input, +3-second offset, 0.6%
progressive drift, and a +5-second jump in the second half. These changes were
injected into copies of the existing subtitle. They do not turn the original
subtitle into a verified timing reference.

The matcher uses distinct dialogue matches, monotonic ordering, coverage guards,
and alternating held-out anchors. Diagnostic fits below may be computed even
when a guard rejects a correction, but those fits do not authorize a file write.
Model-loading/download time is separate from transcription time; matching and
baseline times exclude the common audio decode. ASR memory is process high-water
RSS; ffsubsync memory is sampled peak process-tree RSS. Those definitions differ
and should not be treated as byte-exact equivalent measurements.

The installed Bazarr sync profile is audio-reference mode, WebRTC VAD,
`no_fix_framerate=true`, `gss=true`, and `max_offset_seconds=600`. Primary baseline
results use equivalent CLI options on the same scratch WAV. This compares the
installed algorithm/settings, not Bazarr's end-to-end download or notification
flow. An earlier exploratory run used `--skip-infer-framerate-ratio`; those
results are excluded from the primary comparison because standard framerate
scales were still possible.

## Results

| Local model | Transcribe 480 s | Real-time factor | Peak RSS | Matched cues | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| tiny.en | 51.55 s | 0.107 | 674.6 MiB | 50 / 154 (32.5%) | Abstained: insufficient coverage |
| base.en | 87.15 s | 0.182 | 815.3 MiB | 72 / 154 (46.8%) | Abstained: nonmonotonic matches |
| small.en | 229.50 s | 0.478 | 1298.2 MiB | 90 / 154 (58.4%) | Abstained: insufficient coverage |

All three models abstained on all four input variants. Their matching step took
less than 0.4 seconds per variant, excluding ASR. Download/model-load times were
0.76, 3.20 and 7.11 seconds respectively; the tiny model was already downloaded
by an earlier exploratory run. A package-provisioning attempt overlapped a
portion of the base run; these single-run numbers are preliminary throughput
observations rather than controlled performance confidence intervals.

The larger model increased dialogue coverage but did not provide sufficient
evidence for a global correction. Diagnostic global fits on the unchanged input,
computed without overriding any rejection guard, had maximum held-out residuals
of 1.82 seconds (tiny), 92.65 seconds (base, including a false nonmonotonic match),
and 1.37 seconds (small). Each exceeds the provisional 0.65-second guard. Reducing
the coverage threshold alone would therefore not justify accepting these results.
The tiny model's early anchor offsets had a median of +3.86 seconds, versus
+2.30 seconds later in the sample, consistent with a problem beyond one constant
offset. These are ASR-derived observations, not independently verified cue times.

The installed-profile ffsubsync baseline completed in 1.24–1.33 seconds per
variant and used a sampled peak process-tree RSS of 85.8–88.2 MiB:

| Input variant | Mean absolute delta to original subtitle | P95 delta to original subtitle |
| --- | ---: | ---: |
| Unchanged input | 3.950 s | 3.950 s |
| Injected +3 s | 3.960 s | 3.960 s |
| Injected 0.6% drift | 4.181 s | 5.252 s |
| Injected +5 s second-half jump | 7.258 s | 9.060 s |

These deltas are **not timing errors**: the original subtitle is suspected to be
wrong, and both ffsubsync and ASR evidence suggest an initial positive correction.
The similar first two rows show that the baseline recovered the injected offset
relative to its own preferred alignment. The other rows expose behavior worth
reviewing against independent annotations; they cannot establish which output
sounds correct or whether any change is harmful.

Aggregate machine-readable evidence is in
[benchmark-results-2026-10-29.json](benchmark-results-2026-10-29.json).

## Linux source and operation smoke

The final 30-test source suite passed in 2.875 seconds on Linux Python 3.12.12 as
UID/GID 1000 in a separate disposable pod based on the installed Bazarr image.
Using that image's real ffprobe, `tools/cluster_benchmark_smoke.py` generated a
10-second WAV and SRT in scratch, acknowledged a durable queued job, ran the
disabled-backend worker to review status, previewed a manual correction without
writing, applied it explicitly, and rolled back to identical original bytes.
The smoke pod had no library or production configuration mount.

This is source/runtime validation. It does not establish that the new Dockerfile
was built or that a new application image was deployed. Both temporary pods were
deleted after retaining private reports locally.

## Reproduction and remaining evidence

`tools/cluster_benchmark.py` accepts explicit media, subtitle and scratch paths.
Run it only in an isolated environment with a read-only media mount, an explicit
CPU/RAM budget, model downloads allowed, and private scratch storage. Install the
observed inference dependencies, `webrtcvad-wheels`, and the remaining declared
ffsubsync dependencies before installing ffsubsync 0.4.27 with `--no-deps` if the
native `webrtcvad` build is unavailable. The harness never enables library writes.

Example inside that environment:

```sh
PYTHONPATH=src python tools/cluster_benchmark.py \
  --media /readonly/selected-episode.mkv \
  --subtitle /readonly/selected-episode.en.srt \
  --scratch /scratch/private-run --duration 480 --model small.en
```

Do not publish the resulting transcript or raw reports: they intentionally contain
the owner's private evidence. Publish only an aggregate summary after inspection.

This sample cannot establish harmful-change rates, cue timing accuracy, reliable
confidence thresholds, or acceptable performance across the full library. It
does not cover real ASS/SSA media, already-correct human-verified subtitles,
multiple episodes, different audio tracks, full-episode throughput, or actual
scene-cut repair. Numeric release criteria and independent timing annotations
remain owner decisions. The next quality experiment needs human timing references
across both early and late portions, including held-out points, and an
already-correct control. Larger models alone are not proof of safer alignment.

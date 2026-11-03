# Human timing references for the next benchmark

The first real-media experiment could not measure timing accuracy: the existing
subtitle was suspected to be wrong, and every speech candidate abstained. This
workflow adds sparse human timing references without treating the current
subtitle as ground truth. It does not enable automatic replacement or establish
a release gate.

Use explicitly selected files on read-only media mounts and a private scratch
directory. Run the commands from an installed source environment. Keep
references, transcripts and raw reports under `private/` (gitignored), or outside
the repository. They include dialogue and source fingerprints. No corpus is
bundled, and no library import or whole-library processing is involved.

## Create a review template

Choose the actual English audio stream using ffprobe or your player. The index
below is the absolute container stream index, not the ordinal among audio
streams. `1` is illustrative; use the correct index for the selected media.

```sh
mkdir -p private
python -m bazarr_sync.benchmark \
  --subtitle /readonly/selected.en.srt \
  --media /readonly/selected.mkv --audio-index 1 \
  --make-reference-template --sample-count 12 \
  --output private/reference.json
```

SRT, ASS and SSA use the same reference schema. Templates sample cues across the
subtitle's time range and include its first and last cues when sampling at least
two cues. The cue indices are
zero based. The reference is bound to the subtitle's exact bytes, media SHA-256
and size, and selected audio stream. Hashing reads the entire media once for
template creation and again for scoring; budget for that I/O separately from
alignment time. The command does not decode media or verify that the declared
stream is English, or even that it exists; inspect the selected stream yourself.

Open the JSON in your editor and listen to the selected audio in your normal
player. Enter each reviewed cue's intended `start_seconds` and, when you can
judge it, `end_seconds`, measured on the same media clock from zero. Define and
use a consistent caption-boundary convention; speech-word boundaries alone are
not necessarily the intended caption display boundaries. Leave an unknown
endpoint `null`. Unfilled cues do not contribute to any accuracy metric.

For example, a reviewed annotation might contain:

```json
{
  "cue_index": 42,
  "text": "Example dialogue already supplied by the template",
  "source_start_seconds": 123.0,
  "source_end_seconds": 126.0,
  "start_seconds": 125.4,
  "end_seconds": null
}
```

Keep the generated cue context and source identities intact. Add more cues if
needed by copying their exact context from the same source; do not duplicate an
index. After reviewing, set the top-level `independently_reviewed` field to
`true`. This is the reviewer's declaration, not software verification of human
ground truth. Do not copy the source times or candidate estimates into truth
fields without listening. Collect references before inspecting a candidate's
proposed timings, and do not reuse manual fitting anchors as an independent
test of those same manual corrections.

## Score a candidate and optional ffsubsync baseline

Supply local speech evidence for the same media edit, selected stream, and time
origin. `--transcript` accepts a JSON list of `{ "text": "word", "start": 1.2,
"end": 1.5 }` entries or an object containing that list as `transcript`. Entries
must be ordered and valid. Transcript provenance is caller-supplied; the tool
does not prove which media/stream produced it. A transcript of a cropped window
must have its timestamps translated to the selected media's full time origin.
Do not directly reuse the zero-based 480-second transcript from the earlier
cluster benchmark against a full-episode subtitle.

The application's private transcript cache files already carry an `identity`
object with media SHA-256, audio index and backend/model settings. They can be
used directly as `--transcript`; when that object is present, a mismatch with the
reviewed media/audio stream is rejected. A plain list remains accepted with
explicitly unverified provenance. Reports record input/reference fingerprints
and any supplied transcript identity for reproducibility. Claimed identity is
not proof of transcript authenticity or the correctness of its time origin.

```sh
python -m bazarr_sync.benchmark \
  --subtitle /readonly/selected.en.srt \
  --media /readonly/selected.mkv --audio-index 1 \
  --transcript private/words.json \
  --timing-reference private/reference.json \
  --ffsubsync ffsubsync \
  --ffsubsync-arg=--no-fix-framerate \
  --ffsubsync-arg=--gss \
  --ffsubsync-arg=--max-offset-seconds=600 \
  --output private/reviewed-benchmark.json
```

Omit `--ffsubsync` and its arguments to score only the speech-anchor candidate.
The illustrated profile reproduces the earlier recorded settings; verify the
installed version and profile for a new comparison. The baseline automatically
receives `--reference-stream=0:<audio-index>` and, unless a different VAD was
explicitly supplied, `--vad=webrtc`, to use audio rather than an embedded subtitle
reference. See the upstream [stream selection documentation](https://ffsubsync.readthedocs.io/en/latest/reference_types.html#selecting-a-stream-from-the-reference).
The supported extra flags are `--vad=value`, `--max-offset-seconds=value`,
`--no-fix-framerate`, `--gss`, and `--skip-infer-framerate-ratio`. Record the
installed ffsubsync, FFmpeg, model and inference versions/settings alongside the
report; this command measures matching and baseline runtime, excluding prior
transcription and media hashing.

The baseline gets a scratch subtitle copy and writes its output to scratch.
Annotations are supplied only to scoring, never to the candidate's fitting.
Neither command publishes a subtitle. Output files are created exclusively;
choose a new report filename for each run. Existing files, including source
inputs and their hardlinks, cannot be overwritten through `--output`.

## Read the report

- `timing_reference` records the human-review declaration, annotated cue
  fraction, span across source subtitle time, and early/middle/late cue counts.
  A large span does not imply dense evidence. Unreviewed cues remain unknown.
- Each `reviewed_timing` section reports before/after errors separately for
  starts, ends and all reviewed endpoints: count, mean, median, nearest-rank P95
  and maximum absolute error. Candidate metrics use serialized subtitle
  precision, including ASS/SSA centiseconds.
- Abstentions explicitly report `unchanged_input_on_abstention`; their effective
  output metrics equal input metrics. This is safe abstention, not a successful
  repair. Proposed output is still review-only.
- `diagnostic_harmful_cue_count` counts reviewed cues with any endpoint that was
  within 0.1 seconds of the reference and moves beyond 0.5 seconds. Each cue is
  counted once. These inherited diagnostic tolerances are **not owner-agreed
  release criteria** or a measured harmful-change rate for unreviewed cues.
- Output must preserve all non-timestamp content, format, encoding and BOM.
  Changed wording, cue order, style/header content or encoding prevents scoring;
  the report sets `comparable` to `false` rather than trusting cue-position
  correspondence. A baseline can fail this preservation check even if it sounds
  aligned.

The existing complete-subtitle `--reference trusted.srt` mode remains available
for a separately reviewed file with matching cue text/order. It does not carry
the sparse JSON workflow's source/media/stream binding or review declaration.
Do not combine the two reference modes.

The next actual quality experiment still needs owner-selected failures and an
already-correct control, with independently reviewed points across early and
late sections and around suspected cuts. Repeat across files and formats;
neither one sparse sample nor synthetic tests establishes superiority to
ffsubsync. Numeric release criteria remain an owner decision.

# Local Setup

## Environment

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[twelvelabs]' pytest
```

`ffmpeg` and `ffprobe` are required:

```bash
brew install ffmpeg
```

For local transcription, either install a Whisper-compatible command or import
an externally generated transcript. The CLI supports:

- `whisper` from the optional Python `openai-whisper` package
- `whisper-cli` or `main` from whisper.cpp
- optional local sentence-transformer embeddings for semantic search
- JSON/SRT/VTT transcript import with no ASR dependency

Optional Python Whisper install:

```bash
.venv/bin/python -m pip install -e '.[local-ai]'
```

Optional local face-thumbnail support:

```bash
.venv/bin/python -m pip install -e '.[vision]'
```

On macOS, install the Apple Vision bindings as well. `detect-faces --backend
auto` will use Apple Vision when these bindings are present and fall back to
OpenCV elsewhere:

```bash
.venv/bin/python -m pip install -e '.[vision,macos]'
.venv/bin/tapesplit detect-faces /path/to/family-videos.tapesplit --subject-type event --backend apple-vision
```

For stronger local identity clustering, install the ArcFace/InsightFace backend.
On Apple Silicon, ONNX Runtime can use the Core ML execution provider; Linux and
Windows servers can use CUDA when available:

```bash
.venv/bin/python -m pip install -e '.[vision,macos,face-ai]'
.venv/bin/tapesplit cluster-faces /path/to/family-videos.tapesplit --embedding-backend auto
```

Optional local OCR and visual embeddings:

```bash
.venv/bin/python -m pip install -e '.[vision,macos,visual-ai]'
.venv/bin/tapesplit detect-text /path/to/family-videos.tapesplit --subject-type event --backend auto
.venv/bin/tapesplit caption-visuals /path/to/family-videos.tapesplit --subject-type event --backend auto
.venv/bin/tapesplit embed-visuals /path/to/family-videos.tapesplit --subject-type event --backend auto
.venv/bin/tapesplit build-visual-similarity /path/to/family-videos.tapesplit
.venv/bin/tapesplit classify-content /path/to/family-videos.tapesplit
```

OCR rows are written to `visual_text_observations.jsonl` and become searchable
after `build-evidence` and `search build`. Captions are written to
`visual_captions.jsonl`. Visual embeddings are written to `visual_embeddings.jsonl`;
`build-visual-similarity` turns them into `visual_similarity_edges.jsonl`
candidate same-place/same-event/same-era evidence. `classify-content` writes
`content_classifications.jsonl` for likely family, unrelated, non-content, or
uncertain event-level guesses.

Optional speaker diarization can be imported from RTTM/JSON without installing a
heavy diarization model:

```bash
.venv/bin/tapesplit speakers import /path/to/family-videos.tapesplit speakers.rttm \
  --source-video-id video_000001 \
  --force
```

To run pyannote locally, install the optional dependency and configure a
Hugging Face token accepted for the selected diarization model:

```bash
.venv/bin/python -m pip install -e '.[speaker-ai]'
export HF_TOKEN=...
.venv/bin/tapesplit speakers diarize /path/to/family-videos.tapesplit --source-video-id video_000001
```

For whisper.cpp, download/build whisper.cpp separately and set the model path:

```bash
export WHISPER_CPP_MODEL=/path/to/ggml-large-v3-turbo.bin
```

## Provider Config

Secrets live in `.env`, which is ignored by git.

Required for cloud-hybrid testing:

```bash
TWELVELABS_API_KEY=

AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_REGION=
AZURE_OPENAI_API_VERSION=
AZURE_OPENAI_REASONING_DEPLOYMENT=
AZURE_OPENAI_FAST_DEPLOYMENT=
AZURE_OPENAI_WHISPER_DEPLOYMENT=

GOOGLE_MAPS_API_KEY=
GOOGLE_MAPS_ENABLED=false
GOOGLE_MAPS_DAILY_BUDGET_USD=0

WHISPER_CPP_MODEL=
```

Check config:

```bash
.venv/bin/tapesplit doctor
```

## Current Smoke Tests

Azure OpenAI:

```bash
.venv/bin/tapesplit azure smoke --deployment fast
```

TwelveLabs:

```bash
.venv/bin/tapesplit twelvelabs list-indexes
```

Vertex Gemini:

```bash
gcloud auth application-default login
gcloud config set account you@example.com
gcloud config set project <your-project>
gcloud auth application-default set-quota-project <your-project>

.venv/bin/tapesplit gemini smoke --project /path/to/family-videos.tapesplit
```

## Ingest A Video Folder

```bash
.venv/bin/tapesplit ingest /path/to/family-videos --out /path/to/family-videos.tapesplit
```

This creates:

```text
manifest.json
tapes.jsonl
windows.jsonl
keyframes/
thumbnails/
```

## Local Scene Detection

Detect blue/black/static non-content first, then detect visual scenes:

```bash
.venv/bin/tapesplit detect-non-content /path/to/family-videos.tapesplit
.venv/bin/tapesplit detect-scenes /path/to/family-videos.tapesplit
```

`detect-scenes` uses ffmpeg's scene-change signal and writes `scenes.jsonl`. If
`non_content_ranges.jsonl` exists, blue screens, black screens, and static ranges
become explicit `non_content` scenes and are excluded from customer-facing
search/report output by the visibility filter.

For a bounded test on one tape:

```bash
.venv/bin/tapesplit detect-scenes /path/to/family-videos.tapesplit \
  --source-video-id video_000001 \
  --threshold 0.35 \
  --min-scene-seconds 1.0
```

Extract scene/event thumbnails and optional face thumbnails:

```bash
.venv/bin/tapesplit extract-visuals /path/to/family-videos.tapesplit --force
.venv/bin/tapesplit detect-faces /path/to/family-videos.tapesplit --subject-type scene
.venv/bin/tapesplit cluster-faces /path/to/family-videos.tapesplit
.venv/bin/tapesplit export-visualization /path/to/family-videos.tapesplit
```

`export-visualization` writes `visualization.json`, a UI-ready aggregate for
event timelines, scene timelines, people tracks, place tracks, relationship
graphs, scoped place contexts, visual assets, face clusters, and face-review
queues.

When a reviewer confirms or rejects a candidate, apply those decisions as a JSON
or JSONL correction file:

```bash
.venv/bin/tapesplit review apply /path/to/family-videos.tapesplit review-actions.jsonl
.venv/bin/tapesplit export-visualization /path/to/family-videos.tapesplit
```

This writes `corrections.jsonl` and updates only the targeted projection records.
For example, confirming `home (Madison, Wisconsin context)` does not merge it
with a later `home (Eugene, Oregon context)`.

## Upload To TwelveLabs

Estimate indexing cost before upload:

```bash
.venv/bin/tapesplit costs estimate-twelvelabs-index /path/to/family-videos.tapesplit
```

Use an existing TwelveLabs index id:

```bash
.venv/bin/tapesplit twelvelabs upload /path/to/family-videos.tapesplit --index-id <index_id>
```

For a tiny smoke upload, add `--limit 1`. Add `--wait` only when you want the CLI
to block until indexing is complete.

## LLM Cost Tracking

Token usage is logged when LLM calls are made with a project path:

```bash
.venv/bin/tapesplit azure smoke --deployment fast --project /path/to/family-videos.tapesplit
.venv/bin/tapesplit costs llm /path/to/family-videos.tapesplit
```

To estimate dollars, copy `cost_rates.example.json` to `cost_rates.json` and add
per-million-token rates for each deployment. `cost_rates.json` is ignored by git.

Project-level summary:

```bash
.venv/bin/tapesplit costs project /path/to/family-videos.tapesplit
```

## Gemini Video Analysis

For long VHS/DVD transfers, start with a whole-tape Gemini pass when the video
fits under the upload limit or can be compressed into a proxy. This gives the
model continuity across events, recurring people, repeated places, unrelated
footage, and date/location carryover. Use chunked analysis later as a
high-fidelity follow-up for signs, OCR, confusing transitions, or timestamps
where the whole-tape pass asks for more detail.

Estimate before spending:

```bash
.venv/bin/tapesplit gemini estimate-video /path/to/family-videos.tapesplit \
  --all \
  --fps 1 \
  --media-resolution low \
  --output-tokens 12000
```

Prepare whole-tape analysis files under the Gemini upload size limit:

```bash
.venv/bin/tapesplit gemini prepare-video /path/to/family-videos.tapesplit
```

Run the whole-tape pass, import it into reviewable events/evidence, and export
the static report:

```bash
.venv/bin/tapesplit detect-non-content /path/to/family-videos.tapesplit
.venv/bin/tapesplit detect-scenes /path/to/family-videos.tapesplit
.venv/bin/tapesplit extract-visuals /path/to/family-videos.tapesplit
.venv/bin/tapesplit gemini prepare-video /path/to/family-videos.tapesplit
.venv/bin/tapesplit gemini analyze-video /path/to/family-videos.tapesplit \
  --all \
  --continue-on-error
.venv/bin/tapesplit gemini compare-analyses /path/to/family-videos.tapesplit
.venv/bin/tapesplit rebuild /path/to/family-videos.tapesplit --import-gemini
```

If you only want to retry videos already uploaded to TwelveLabs, add
`--uploaded-to-twelvelabs-only` to the `estimate-video` and `analyze-video`
commands. Existing chunked results are not overwritten; whole-tape analyses are
appended to `gemini_analyses.jsonl` and can be imported alongside prior runs.

`gemini summarize-analyses` gives a no-spend coverage view by run/video/mode.
`gemini compare-analyses` compares chunked runs against whole-tape runs once
both exist, including whole-only people, places, dates, and follow-up signals.
`rebuild --import-gemini` imports the selected Gemini run and regenerates
canonical events, groups, place roles, relationships, context graph edges,
search, report, and visualization output in one local step.

`stitch-events` creates `canonical_events.jsonl`. It keeps whole-tape and
chunk-derived event candidates tied back to their raw provenance through
`source_event_ids`.

`build-groups` is local and deterministic. It creates reviewable album, event,
people, place, date, and language projections from `canonical_events.jsonl`
without making provider calls:

```text
albums.jsonl
event_groups.jsonl
people_groups.jsonl
place_groups.jsonl
date_groups.jsonl
language_groups.jsonl
```

Generated chunk clips, raw Gemini responses, and reports stay inside the ignored
`.tapesplit` project directory. Originals are not modified.

If a chunk fails because Gemini returns malformed or truncated JSON, continue the
same run after adjusting token limits:

```bash
.venv/bin/tapesplit gemini analyze-video-chunks /path/to/family-videos.tapesplit \
  --run-id gem_run_... \
  --start-chunk 3 \
  --max-output-tokens 12000
```

## Local Transcription

Extract audio only:

```bash
.venv/bin/tapesplit transcribe extract-audio /path/to/family-videos.tapesplit \
  --source-video-id video_000001
```

Run a local Whisper-compatible CLI. Omit `--language` for auto-detection; pass
`ru`, `en`, or another language code when a tape is mostly one language:

```bash
.venv/bin/tapesplit transcribe local /path/to/family-videos.tapesplit \
  --source-video-id video_000001 \
  --engine auto \
  --language ru \
  --force
```

Import an existing transcript instead:

```bash
.venv/bin/tapesplit transcribe import /path/to/family-videos.tapesplit transcript.srt \
  --source-video-id video_000001 \
  --language ru \
  --force
```

All transcript paths write `transcript_segments.jsonl`. Running
`build-evidence` after transcription also promotes transcript segments into
`evidence.jsonl` for downstream claim extraction.

## Local Search

Build a local SQLite search index:

```bash
.venv/bin/tapesplit search build /path/to/family-videos.tapesplit
```

Use local neural embeddings when `sentence-transformers` is installed:

```bash
.venv/bin/tapesplit search build /path/to/family-videos.tapesplit \
  --embedding-backend sentence-transformers \
  --embedding-model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

Query transcript segments, evidence, canonical events, albums, and group indexes:

```bash
.venv/bin/tapesplit search query /path/to/family-videos.tapesplit "first day of school"
.venv/bin/tapesplit search query /path/to/family-videos.tapesplit "lava flowing into the ocean"
```

Find records similar to an existing indexed record:

```bash
.venv/bin/tapesplit search similar /path/to/family-videos.tapesplit canonical_event_000080 --record-type event
.venv/bin/tapesplit search similar /path/to/family-videos.tapesplit place_group_000003 --record-type place_group
```

The default index combines SQLite FTS5 text search with a dependency-free local
sparse vector scorer and small domain synonym expansions. The optional
`sentence-transformers` backend adds local neural embeddings without sending
text to a provider. Similarity search uses sparse vectors by default and also
uses dense vectors when the index was built with `--embedding-backend
sentence-transformers`. If `sqlite-vec` is installed, dense vectors are also
indexed inside `search.sqlite` as a local vector table.

## Family Review Evaluation

Build a reviewer packet after event stitching, grouping, relationship extraction,
and context-graph construction:

```bash
.venv/bin/tapesplit eval build /path/to/family-videos.tapesplit --force
```

The packet contains JSONL, CSV, and SQLite artifacts under
`eval_packet/`. A non-technical reviewer can copy
`annotations.template.csv` to `annotations.csv`, fill in judgments and notes,
and mark uncertain items as `needs_followup`.

Score the completed annotations:

```bash
.venv/bin/tapesplit eval score /path/to/family-videos.tapesplit
```

Scoring writes `eval_report.json`, `scored_items.jsonl`, and
`family_followups.jsonl`, and updates `eval.sqlite` with scored annotation
tables. See `docs/EVALUATION_WORKFLOW.md` for the full reviewer, SQL, and
prompting workflow.

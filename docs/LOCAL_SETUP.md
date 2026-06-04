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

For long VHS/DVD transfers, prefer chunked analysis over a single whole-tape
call. Chunk outputs preserve both chunk-local time and absolute source-video
time, which makes review/report links safer when Gemini timestamps drift.

Estimate before spending:

```bash
.venv/bin/tapesplit gemini estimate-video /path/to/family-videos.tapesplit \
  --chunk-seconds 900 \
  --chunk-overlap-seconds 15 \
  --fps 1 \
  --media-resolution low \
  --output-tokens 8000
```

Run a bounded first chunk:

```bash
.venv/bin/tapesplit gemini analyze-video-chunks /path/to/family-videos.tapesplit \
  --chunk-seconds 900 \
  --chunk-overlap-seconds 15 \
  --limit-chunks 1
```

Run the full chunk pass, import it into reviewable events/evidence, and export
the static report:

```bash
.venv/bin/tapesplit gemini analyze-video-chunks /path/to/family-videos.tapesplit \
  --chunk-seconds 900 \
  --chunk-overlap-seconds 15
.venv/bin/tapesplit gemini import-analysis /path/to/family-videos.tapesplit
.venv/bin/tapesplit stitch-events /path/to/family-videos.tapesplit
.venv/bin/tapesplit build-groups /path/to/family-videos.tapesplit
.venv/bin/tapesplit search build /path/to/family-videos.tapesplit
.venv/bin/tapesplit export-report /path/to/family-videos.tapesplit
```

`stitch-events` creates `canonical_events.jsonl`. It prefers Gemini chunk events
when available and keeps raw event candidates as provenance through
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

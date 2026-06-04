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
.venv/bin/tapesplit export-report /path/to/family-videos.tapesplit
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

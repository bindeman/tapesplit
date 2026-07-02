# TapeSplit

TapeSplit is a local-first VHS and home-video intelligence layer.

It takes unorganized digitized tapes (long VHS/DVD/camcorder transfers) and
automatically produces a reviewable modern archive:

- canonical events, albums, and a browsable timeline
- likely dates, people, places, relationships, and related/unrelated labels
- transcript, frame, face, speaker, OCR, and visual evidence with provenance
- multilingual transcript preservation and semantic search over moments
- a local review UI where every remaining guess is one click to correct
- durable corrections that survive re-runs

The system defaults to best guesses with provenance ("appears to be …"),
auto-accepts only well-corroborated inferences, and keeps everything else one
click away in the review UI. It runs fully locally on a Mac (Apple Vision OCR
and face detection, whisper.cpp transcription, pyannote/speechbrain
diarization, ArcFace face identity, BLIP captions), with an optional
Vertex Gemini backend for deep video understanding.

## Quickstart

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[local-ai,vision,macos,visual-ai,face-ai,speaker-ai]'
brew install ffmpeg

.venv/bin/tapesplit doctor                      # what will run on this machine
.venv/bin/tapesplit auto /path/to/tape-folder   # tapes in, archive out
.venv/bin/tapesplit ui /path/to/tape-folder.tapesplit
```

`tapesplit auto` runs the whole pipeline end to end:

ingest → non-content/scene detection → transcription → speaker diarization →
(optional) Gemini video analysis → event stitching → keyframes → OCR →
captions → visual embeddings → faces → clustering → groups/relationships →
search index → story/report/visualization → safe auto-acceptance of
high-confidence suggestions.

Key properties:

- **Capability aware.** Stages that lack a backend (no cloud keys, missing
  optional ML package, non-macOS) are skipped with a reason instead of
  failing; the archive is built from whatever evidence exists. `tapesplit
  doctor` shows per-stage readiness.
- **Resumable.** Per-stage state lives in `pipeline_state.json` inside the
  project; re-running `auto` continues where it stopped, and per-video stages
  skip already-processed videos. `tapesplit status <project>` shows progress.
- **Cost guarded.** Cloud analysis is estimated first and skipped when the
  estimate exceeds `--max-cloud-usd` (default $10).
- **Local events without cloud.** Tapes with no cloud analysis still get
  low-confidence "recording segment" events synthesized from scene/non-content
  boundaries and transcript keywords, so the timeline is never empty.
- **Minimal-intervention.** After the build, high-confidence suggestions are
  auto-accepted as durable corrections under a per-action-type safety policy
  (`--no-suggestions` to disable). Relationships require resolved identities
  on both sides, no contradicting evidence, and corroboration before
  auto-accept.

Useful variants:

```bash
tapesplit auto INPUT --plan                 # show what would run, then exit
tapesplit auto INPUT --profile local        # never call cloud providers
tapesplit auto INPUT --profile minimal      # deterministic stages only
tapesplit auto INPUT --max-cloud-usd 25 --language ru
tapesplit auto INPUT --force-from scenes    # redo scenes and everything after
tapesplit auto INPUT --skip captions --skip diarize
tapesplit status PROJECT                    # stage state + archive metrics
tapesplit synthesize-events PROJECT         # heuristic events on demand
```

## Review UI

```bash
.venv/bin/tapesplit ui /path/to/project.tapesplit
```

The UI supports timeline, albums, people, places, search, and review views,
source-video playback by timestamp, a pending correction queue, and one-click
`Accept Best Guesses` (which uses the same safe policy). Corrections are
durable: `tapesplit auto`/`rebuild` replays them after every regeneration.

## Cloud providers (optional)

Copy `.env.example` to `.env`. Vertex Gemini video analysis uses gcloud ADC:

```bash
gcloud auth application-default login
gcloud config set project <your-project>
```

`GEMINI_PROJECT_BUDGET_USD` caps cumulative spend; `tapesplit auto` also
pre-estimates each run against `--max-cloud-usd`. TwelveLabs indexing and
Azure OpenAI claim extraction remain available as manual commands.

## Manual pipeline commands

Every stage is still exposed as an individual command (`ingest`,
`detect-non-content`, `detect-scenes`, `extract-visuals`, `transcribe local`,
`speakers diarize`, `gemini analyze-video`, `rebuild`, `detect-text`,
`caption-visuals`, `embed-visuals`, `build-visual-similarity`,
`classify-content`, `detect-faces`, `cluster-faces`, `search build`,
`export-visualization`, `review apply-suggestions`, …). See
`docs/LOCAL_SETUP.md` and `tapesplit --help`.

Local transcripts can also be imported from JSON/SRT/VTT
(`transcribe import`), and speaker segments from JSON/RTTM
(`speakers import`), so the pipeline works even with zero ML dependencies.

Search:

```bash
.venv/bin/tapesplit search query /path/to/project.tapesplit "first day of school"
.venv/bin/tapesplit search similar /path/to/project.tapesplit canonical_event_000001 --record-type event
```

`search build` defaults to SQLite FTS5 plus a dependency-free local sparse
vector scorer; install the `local-ai` extra for dense sentence-transformer
embeddings with `sqlite-vec` ANN indexing. See
`docs/RETRIEVAL_ARCHITECTURE.md` for the hybrid search design.

Evaluation packets for family review:

```bash
.venv/bin/tapesplit eval build /path/to/project.tapesplit --force
.venv/bin/tapesplit eval score /path/to/project.tapesplit --annotations annotations.csv
```

## Docs

- [docs/HANDOFF.md](docs/HANDOFF.md) — blind-takeover entry point for agents/contributors
- [docs/AUTOMATION.md](docs/AUTOMATION.md) — `tapesplit auto` design: stages, state, degradation, policies
- [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) — install, provider config, per-command reference
- [docs/MVP_ENGINEERING_PLAN.md](docs/MVP_ENGINEERING_PLAN.md) — MVP shape and rationale
- [docs/TEMPORAL_EVIDENCE_FABRIC.md](docs/TEMPORAL_EVIDENCE_FABRIC.md) — long-term data model
- [docs/ENTITY_RESOLUTION.md](docs/ENTITY_RESOLUTION.md) — alias/role/merge strategy
- [docs/RELATIONSHIP_INFERENCE.md](docs/RELATIONSHIP_INFERENCE.md) — family-graph inference
- [docs/LOCAL_FACE_STACK.md](docs/LOCAL_FACE_STACK.md) — Apple Vision + ArcFace identity plan
- [docs/LOCAL_MODEL_UPGRADE_PLAN.md](docs/LOCAL_MODEL_UPGRADE_PLAN.md) — local model roadmap
- [docs/CONTEXT_GRAPH_VISUALIZATION.md](docs/CONTEXT_GRAPH_VISUALIZATION.md) — context graph model
- [docs/VISUALIZATION_BACKEND.md](docs/VISUALIZATION_BACKEND.md) — UI data model and review queues
- [docs/EVALUATION_WORKFLOW.md](docs/EVALUATION_WORKFLOW.md) — eval packet + scoring workflow

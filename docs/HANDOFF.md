# TapeSplit Agent Handoff

This file is the blind-takeover entry point. Use it before reading the longer
design docs or chat history.

## Current State

- Repo: `/path/to/tapesplit`
- GitHub remote: `https://github.com/bindeman/tapesplit`
- Project type: local-first, source-available VHS and home-video intelligence backend
  with a React/Vite review UI and a one-command `tapesplit auto` orchestrator.
- Sample project: `examples/family-haul.tapesplit`
- Secrets: never commit `.env`, tokens, raw media, project outputs, caches, or
  `cost_rates.json`.

The codebase is versioned and pushed. The worktree should normally be clean
before a new task starts.

## Product Goal

TapeSplit takes digitized VHS/DVD/home-video files and produces a reviewable
modern archive:

- canonical events and albums
- likely dates, places, people, relationships, and related/unrelated labels
- transcript, frame, face, speaker, OCR, and visual evidence
- semantic search over events, moments, people, places, and evidence
- corrections that survive reruns
- UI-ready visualization data for a future desktop app

The system should default to best guesses with provenance, then make correction
fast in the UI. Use phrasing like "appears to be" for inferred facts.

## Main Architecture

The project is a Python CLI plus a local review UI.

- CLI entry point: `src/tapesplit/cli.py`
- Core project artifacts: JSONL files inside a `.tapesplit` project directory
- Visualization export: `visualization.json`
- Search index: `search.sqlite`
- Review UI: `apps/review-ui`
- Durable reviewer corrections: `corrections.jsonl`

The backend is intentionally provider-agnostic. Gemini/TwelveLabs/Azure can be
used for cloud-hybrid analysis, while local macOS/Linux/Windows components are
being added for OCR, faces, transcription, diarization, visual embeddings, and
search.

## Important Docs

Read these first:

- `docs/LOCAL_SETUP.md`: install, provider config, pipeline commands
- `docs/MVP_ENGINEERING_PLAN.md`: current MVP shape and rationale
- `docs/VISUALIZATION_BACKEND.md`: UI data model and review queues
- `docs/ENTITY_RESOLUTION.md`: aliases, roles, names, and merge strategy
- `docs/RELATIONSHIP_INFERENCE.md`: family/social graph inference
- `docs/LOCAL_MODEL_UPGRADE_PLAN.md`: local model stack plan
- `docs/EVALUATION_WORKFLOW.md`: future human-review/eval workflow

## Setup

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[twelvelabs,local-ai,vision,macos,visual-ai,face-ai,speaker-ai]' pytest
brew install ffmpeg
npm --prefix apps/review-ui install
```

For a lighter install, see `docs/LOCAL_SETUP.md`.

Provider secrets belong in `.env`. Use `.env.example` as the schema. Google
Vertex Gemini uses ADC rather than API keys in the current setup:

```bash
gcloud auth application-default login
gcloud config set project <your-project>
gcloud auth application-default set-quota-project <your-project>
```

## Verify The Repo

```bash
.venv/bin/python -m pytest -q
npm --prefix apps/review-ui run build
git diff --check
```

Expected at this handoff: `166 passed`, UI build passes, and `git diff --check`
has no output.

## Current Sample Project Snapshot

`examples/family-haul.tapesplit` was fully processed by `tapesplit auto` on
2026-07-02 (all 19 tapes / 31.4 hours Gemini-analyzed, ~$4.2 incremental
cloud spend) and now has:

- source videos: 19 (19/19 transcribed, diarized, and Gemini-analyzed)
- canonical event rows: 311 (0 heuristic — all superseded by analysis)
- visualization timeline events: 195
- people in visualization: 72
- places: 181
- face observations / clusters: 1230 / 714
- speaker segments: 5540
- scenes: 9345, non-content ranges: 2060, visual assets: 4525
- durable corrections applied: 181 (auto-accepted under the safe policy)
- primary review items remaining: 60

The full history of the run (four passes, including the Vertex failure modes
that drove the chunk-routing/retry/coverage fixes) is in the git log around
2026-07-02.

Dry-running bulk primary suggestions selects 20 correction actions with
`--policy legacy`, and 12 with the default `--policy safe` (unbridged role
relationships and sub-floor confidences are skipped with reasons):

```bash
.venv/bin/tapesplit review apply-suggestions examples/family-haul.tapesplit \
  --tier primary \
  --dry-run
```

The selected actions include face identity, speaker identity, person confirms,
role merges, place context, place confirmation, and relationship confirmations.
Do not apply them to the sample blindly if you are trying to preserve the current
baseline; dry-run and inspect first.

## Review UI

Start the UI against the sample project:

```bash
TAPESPLIT_PROJECT=examples/family-haul.tapesplit \
  npm --prefix apps/review-ui run dev -- --host 127.0.0.1
```

Vite will print the local URL, usually `http://127.0.0.1:5173/` or the next free
port.

The UI supports:

- timeline, albums, people, places, search, and review views
- source video playback by timestamp
- pending correction queue
- one-click `Accept Best Guesses` for the selected review tier
- correction replay through the local dev API

## Core Pipeline

The whole pipeline is one command (see `docs/AUTOMATION.md` for the design):

```bash
.venv/bin/tapesplit auto /path/to/tapes-or-project     # runs everything below
.venv/bin/tapesplit auto /path/to/project.tapesplit --plan   # dry-run plan
.venv/bin/tapesplit status /path/to/project.tapesplit  # stage state + metrics
```

`auto` is resumable (per-stage state in `pipeline_state.json`), capability
aware (missing backends skip with a reason), cost guarded
(`--max-cloud-usd`, default $10), and finishes by auto-accepting
high-confidence suggestions under the safe policy (`--no-suggestions`
to disable). Tapes without cloud analysis get low-confidence heuristic
"recording segment" events so the timeline is never empty.

Every stage also remains an individual command for manual/partial runs:

```bash
.venv/bin/tapesplit detect-non-content /path/to/project.tapesplit
.venv/bin/tapesplit detect-scenes /path/to/project.tapesplit
.venv/bin/tapesplit extract-visuals /path/to/project.tapesplit --force
.venv/bin/tapesplit gemini prepare-video /path/to/project.tapesplit
.venv/bin/tapesplit gemini estimate-video /path/to/project.tapesplit --all --output-tokens 12000
.venv/bin/tapesplit gemini analyze-video /path/to/project.tapesplit --all --continue-on-error
.venv/bin/tapesplit gemini compare-analyses /path/to/project.tapesplit
.venv/bin/tapesplit rebuild /path/to/project.tapesplit --import-gemini
.venv/bin/tapesplit detect-text /path/to/project.tapesplit --subject-type event --backend auto
.venv/bin/tapesplit caption-visuals /path/to/project.tapesplit --subject-type event --backend auto
.venv/bin/tapesplit embed-visuals /path/to/project.tapesplit --subject-type event --backend auto
.venv/bin/tapesplit build-visual-similarity /path/to/project.tapesplit
.venv/bin/tapesplit classify-content /path/to/project.tapesplit
.venv/bin/tapesplit detect-faces /path/to/project.tapesplit --subject-type scene --backend auto
.venv/bin/tapesplit cluster-faces /path/to/project.tapesplit --embedding-backend auto
.venv/bin/tapesplit speakers diarize /path/to/project.tapesplit --source-video-id video_000001
.venv/bin/tapesplit speakers identify /path/to/project.tapesplit
.venv/bin/tapesplit search build /path/to/project.tapesplit
.venv/bin/tapesplit export-visualization /path/to/project.tapesplit
```

Use `rebuild --import-gemini` to regenerate local derived outputs and replay
existing `corrections.jsonl`. `rebuild` now also synthesizes heuristic events
for uncovered tapes (disable with `synthesize_heuristic_events=False`).

## Review Corrections

Manual actions:

```bash
.venv/bin/tapesplit review apply /path/to/project.tapesplit review-actions.jsonl
.venv/bin/tapesplit review list /path/to/project.tapesplit
.venv/bin/tapesplit review reapply /path/to/project.tapesplit
```

Bulk best-guess actions:

```bash
.venv/bin/tapesplit review apply-suggestions /path/to/project.tapesplit --tier primary --dry-run
.venv/bin/tapesplit review apply-suggestions /path/to/project.tapesplit --tier primary
.venv/bin/tapesplit review apply-suggestions /path/to/project.tapesplit --policy legacy  # old flat behavior
```

`apply-suggestions` reads `visualization.json`, converts `suggested_action`
objects into durable review corrections, expands grouped relationship actions,
skips already closed or stale targets, and appends to `corrections.jsonl`.

The default `--policy safe` adds per-action confidence floors and
relationship corroboration gates (both sides resolved to people groups, no
contradicting evidence, multi-mention or >=0.85 confidence). On the sample
project this selects 12 of the 20 legacy actions and skips all unbridged
role relationships. `tapesplit auto` runs this automatically as its last
stage (reviewer `auto-pipeline`).

## Current Capabilities

Implemented and tested:

- `tapesplit auto`: one-command, capability-aware, resumable, cost-guarded
  end-to-end pipeline (see `docs/AUTOMATION.md`); plus `status`, `ui`,
  `synthesize-events`, and an extended `doctor` with per-stage readiness
- heuristic local events for tapes without cloud analysis, stitched at
  lowest precedence and superseded automatically by later Gemini runs
- safe auto-accept policy: per-action confidence floors + relationship
  corroboration gates, used by the CLI, the `auto` pipeline, and UI bulk accept
- VHS non-content range detection for blue/black/static sections
- scene detection
- Gemini whole-video/chunk analysis, import, compare, and cost tracking
- event stitching/reconciliation/alignment
- grouping for events, albums, people, places, dates, languages
- scoped place contexts such as multiple homes across eras
- local OCR, captions, visual embeddings, visual similarity, content classifier
- Apple Vision face detection path on macOS with OpenCV fallback
- ArcFace/InsightFace-style local face embedding backend path
- local transcription/import and multilingual search
- speaker diarization with pyannote attempt and SpeechBrain fallback
- speaker identity candidates using transcript/event/face/relationship context
- local hybrid text/vector search
- review UI with video playback and bulk suggestion acceptance
- durable corrections and correction replay on rebuild

## Known Weak Spots

These are the highest-risk areas for the next agent:

- Relationship candidates can be too eager when they are based only on kinship
  words near names. Example: unresolved father/mother/grandparent candidates
  may need more identity bridging before being accepted automatically.
- Place drift still needs stronger safeguards. One known failure mode was an
  Alaska mention contaminating an event that visually/contextually belonged to
  Madison, Wisconsin.
- Generic places such as `home`, `park`, `lake`, `classroom`, or `apartment
  complex` need continuity, era, and parent-place scoping before GPS/export.
- Face thumbnails exist, but low-quality crops and name/role duplicates still
  require better clustering and UI confidence handling.
- Speaker identity is useful but still draft. Treat it as a reviewable signal,
  not fact.
- Evals are designed but not yet the main development loop.
- Bulk `Accept Best Guesses` reduces clicks, but it should eventually use
  stronger per-action thresholds and maybe skip weak relationship categories by
  default.

## Best Next Steps

Good continuation points:

1. ~~Add safer auto-accept thresholds by action type.~~ Done: `--policy safe`
   with relationship identity-bridge/corroboration gates. Next: tune floors
   against eval packets, and consider auto-accepting `merge_person` role
   bridges first so gated relationships unlock on a second `auto` pass.
2. Add a project-memory layer for reviewed aliases, known residences by era,
   relationship facts, and recurring place labels.
3. Improve role resolution so `Mom`, `mother`, `Katya`, and `Ekaterina` can merge
   when event, transcript, relationship, speaker, and face signals agree.
4. Build eval packets for the sample family and make a quick reviewer workflow
   for validating people, places, relationships, and event labels.
5. Improve location normalization and candidate display in the UI, including
   evidence tooltips and "from context" labels.
6. Extend video search/moments so every event, person, place, and relationship
   links back to playable timestamps.
7. `auto` follow-ups: `ingest --append` for growing projects, parallel
   per-video transcribe/diarize, TwelveLabs as an opt-in cloud stage, and a
   `--watch` mode that picks up new tapes dropped into a folder.

## Development Rules

- Preserve user/media data. Do not delete generated project artifacts unless the
  task explicitly asks for regeneration or cleanup.
- Do not commit secrets or raw media.
- Prefer local/deterministic modules for VHS-specific logic; keep providers
  swappable.
- Keep corrections durable and replayable.
- Add tests for any change that affects review decisions, entity resolution,
  relationship inference, places, or export-visible metadata.

# TapeSplit

TapeSplit is a local-first VHS and home-video intelligence layer.

The goal is to take long digitized tapes or DVD transfers and produce:

- scene and event groups
- likely dates, people, places, and event labels
- family vs unrelated-footage classification
- transcript-backed and frame-backed evidence
- multilingual transcript preservation and translation
- semantic search over clips and moments
- reviewable metadata and album-ready exports

The first MVP is designed for a 128 GB M3 Max MacBook Pro with an optional cloud
LLM or video-intelligence backend. The core VHS-specific logic should remain
backend-agnostic so it can run locally or call cloud providers depending on the
deployment.

See [docs/MVP_ENGINEERING_PLAN.md](docs/MVP_ENGINEERING_PLAN.md) for the current
one-week build plan.

See [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) for local setup and provider
smoke-test commands.

See [docs/TEMPORAL_EVIDENCE_FABRIC.md](docs/TEMPORAL_EVIDENCE_FABRIC.md) for
the longer-term data model and reasoning architecture.

See [docs/ENTITY_RESOLUTION.md](docs/ENTITY_RESOLUTION.md) for the alias,
relationship, and role-resolution model.

See [docs/RELATIONSHIP_INFERENCE.md](docs/RELATIONSHIP_INFERENCE.md) for the
family-graph, relationship-candidate, face-linking, and thumbnail plan.

See [docs/CONTEXT_GRAPH_VISUALIZATION.md](docs/CONTEXT_GRAPH_VISUALIZATION.md)
for the longer-term family tree, friend/social-circle, era, and context graph
visualization model.

See [docs/PHOTO_ARCHIVE_PRODUCT.md](docs/PHOTO_ARCHIVE_PRODUCT.md) for a
separate photo-library family-graph product concept that reuses the same
evidence/review architecture.

Current local review pipeline:

```bash
.venv/bin/tapesplit ingest /path/to/video-or-folder
.venv/bin/tapesplit gemini analyze-video-chunks /path/to/project.tapesplit
.venv/bin/tapesplit gemini import-analysis /path/to/project.tapesplit
.venv/bin/tapesplit stitch-events /path/to/project.tapesplit
.venv/bin/tapesplit build-groups /path/to/project.tapesplit
.venv/bin/tapesplit build-relationships /path/to/project.tapesplit
.venv/bin/tapesplit search build /path/to/project.tapesplit
.venv/bin/tapesplit export-report /path/to/project.tapesplit
```

Local transcript segments can come from a Whisper-compatible CLI or from an
existing JSON/SRT/VTT file:

```bash
.venv/bin/tapesplit transcribe local /path/to/project.tapesplit --language ru
.venv/bin/tapesplit transcribe import /path/to/project.tapesplit transcript.srt --source-video-id video_000001
.venv/bin/tapesplit search query /path/to/project.tapesplit "first day of school"
```

`search build` defaults to SQLite FTS5 plus a dependency-free local sparse vector
scorer. Install the `local-ai` extra and pass `--embedding-backend
sentence-transformers` when you want local neural embeddings.

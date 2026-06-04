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

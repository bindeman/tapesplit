# TapeSplit Automation Design

`tapesplit auto` is the one-command orchestrator: unorganized tapes in,
reviewable archive out, with no human input required. This doc describes its
design so stages can be added or tuned safely. Implementation:
`src/tapesplit/auto.py`; tests: `tests/test_auto.py`,
`tests/test_auto_accept_policy.py`, `tests/test_heuristic_events.py`.

## Goals

1. **Zero-babysitting runs.** A run either produces a usable archive or a
   clear per-stage account of what could not be done and why.
2. **Minimal human confirmation.** The pipeline connects transcript, face,
   speaker, place, and relationship signals, then auto-accepts inferences that
   clear per-action safety gates. Humans review only genuinely ambiguous
   items.
3. **Local-first with graceful degradation.** Cloud (Vertex Gemini) improves
   quality but is optional and cost-guarded. Every stage has a local path or
   is skipped with a reason.

## Stage graph

Stages are declared in `build_stages()` with:

- `requires` — hard dependencies; if one did not complete, the stage is
  blocked (recorded, not fatal).
- `after` — soft ordering only; the stage runs whether or not those ran.
- `produces` — artifact files used for done-detection when no state exists
  (lets `auto` adopt projects built with manual commands).
- `availability(capabilities)` — can this stage run on this machine?
- `kind` — `setup` / `local` / `local-ml` / `cloud` / `derived`; profiles
  disable kinds (`--profile local` drops `cloud`, `minimal` also drops
  `local-ml`).
- `always_run` — derived rebuild stages are cheap and idempotent, so they run
  on every invocation to fold in whatever new evidence appeared.

Current order:

| # | stage | kind | needs |
|---|-------|------|-------|
| 1 | ingest | setup | ffprobe |
| 2 | exif | local | exiftool |
| 3 | non-content | local | ffmpeg |
| 4 | scenes | local | ffmpeg (after non-content) |
| 5 | transcribe | local-ml | whisper.cpp + `WHISPER_CPP_MODEL`, or openai-whisper |
| 6 | diarize | local-ml | pyannote (+`HF_TOKEN`) or speechbrain fallback |
| 7 | gemini | cloud | Vertex ADC + `GEMINI_GCS_BUCKET`; estimate gated by `--max-cloud-usd` |
| 8 | core-build | derived | rebuild `core_only`: gemini import → evidence → heuristic events → stitch → classify |
| 9 | visuals | local | keyframes/thumbnails for scenes + events (events exist after core-build) |
| 10–15 | ocr, captions, visual-embed, visual-similarity, faces, face-cluster | local-ml | per-backend |
| 16 | finalize | derived | full rebuild: groups, place roles, alignments, reconciliation, relationships, speaker identities, context graph, search, story, report, visualization |
| 17 | apply-suggestions | derived | safe-policy auto-acceptance + output refresh |

## State and resume

`pipeline_state.json` (in the project dir) records per-stage
status/timing/summary/error. Rules:

- A stage with recorded `completed` status is skipped (`--force` /
  `--force-from STAGE` override).
- With no state but all `produces` artifacts present, the stage is adopted as
  done (supports pre-orchestrator projects).
- Per-video stages additionally skip already-processed videos by reading their
  own artifacts (transcribe uses `skip_existing`; diarize/gemini filter tapes
  already present in `speaker_segments.jsonl`/`gemini_analyses.jsonl`), so an
  interrupted stage resumes mid-batch.
- Stage failures record the error + traceback tail and continue; hard
  dependents are blocked. `ok` in the result means ingest + core-build +
  finalize completed.

## Cost guard

Before analyzing, the gemini stage estimates the pending (not yet analyzed)
videos with `estimate_project_videos`. If the estimate exceeds
`--max-cloud-usd` (default $10) — or cannot be computed while a cap is set —
the stage is *skipped* (not failed) with the amount in the reason. The
adapter-level `GEMINI_PROJECT_BUDGET_USD` cumulative budget still applies
underneath.

## Heuristic local events

Cloud analysis is the only source of rich events. Without it, tapes would
have no timeline at all, so `heuristic_events.py` synthesizes low-confidence
"recording segment" events per tape from local signals:

- content spans from `scenes.jsonl` (which folds in non-content ranges), else
  the complement of `non_content_ranges.jsonl`, else the whole tape;
- titles/summaries from transcript keywords in-range (proper nouns boosted);
- confidence fixed at 0.25, `source: local_heuristic`, `heuristic: true`.

Stitching (`event_stitching.load_source_events`) includes heuristic events
only for tapes that no analyzed (Gemini/Azure) event touches, so a later
cloud run automatically supersedes them. Synthesis also runs inside
`rebuild`, keeping local projects self-healing.

## Safe auto-acceptance

`review apply-suggestions --policy safe` (the default, also used by the
pipeline and the UI's bulk accept) applies per-action-type gates on top of
tier selection:

- Confidence floors per action (`SAFE_AUTO_ACCEPT_MIN_CONFIDENCE`), e.g.
  `confirm_identity` ≥ 0.75, `confirm_person` ≥ 0.6, `confirm_event` ≥ 0.55.
  Conservative/reversible actions (`mark_role_only`,
  `mark_historical_context`) have no floor.
- Relationships additionally require:
  - both `subject_entity_id` and `object_entity_id` resolved to
    `people_group_*` (an unresolved `role_entity_*` side means the
    role-identity bridge has not happened yet);
  - no `contradicting_evidence_ids`;
  - ≥ 2 supporting evidence ids, or candidate confidence ≥ 0.85.

`--policy legacy` restores the old flat tier/min-confidence behavior. Skipped
suggestions carry a `safe policy: <reason>` entry in the result so the review
UI can explain why something still needs a human.

After acceptance mutates artifacts, the stage re-runs the derived rebuild so
exports, search, and the review queue reflect the accepted guesses.

## Extending

To add a stage: write a run function taking `StageContext`, declare the
`Stage` in `build_stages()` with dependencies/availability, and add a planner
test in `tests/test_auto.py`. Keep run functions idempotent and let them
raise `StageSkipped("reason")` for intentional no-ops (e.g. no faces found).

Known future work:

- `ingest --append` for adding tapes to an existing project (currently the
  tape set is fixed at ingest).
- Parallel per-video execution for transcribe/diarize on multi-core machines.
- Project-memory layer (reviewed aliases, residences by era) feeding the
  safety gates.
- TwelveLabs indexing as an opt-in cloud stage.

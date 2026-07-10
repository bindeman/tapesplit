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
| 18 | semantic-embed | local-ml | dense multilingual + CLIP vectors for semantic search (incremental) |
| 19 | verify | cloud | blind clip verification of sampled claims (`TAPESPLIT_VERIFIER_BACKEND`) |

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

### Closed-loop calibration

Every review decision is ground truth about the system's suggestions, and
`src/tapesplit/calibration.py` closes the loop:

- human confirmations count for a suggestion type; human rejections count
  against it; an auto-accepted correction later rejected or edited by a human
  counts double against it (automation asserted something a person had to
  undo).
- `tapesplit review calibrate <project>` computes Laplace-smoothed precision
  per action type from `corrections.jsonl` and tunes the safe-policy floors
  toward per-type precision targets (e.g. 0.95 for identities and
  relationships), bounded per type and only once ≥8 human observations
  exist. Results are written to `review_policy.json` in the project.
- `apply_review_suggestions --policy safe` reads the tuned floors, and the
  `auto` pipeline recalibrates immediately before each acceptance stage — so
  every correction a reviewer makes tightens or relaxes what the next run
  accepts automatically, per archive, with no model retraining.

After acceptance mutates artifacts, the stage re-runs the derived rebuild so
exports, search, and the review queue reflect the accepted guesses.

## Verification loop

`src/tapesplit/verification.py` closes a second, human-free loop by checking
the pipeline's claims against the footage itself. Generating claims over
hours of tape is hard; checking one claim against a short clip is nearly
trivial for a multimodal model — verification exploits that asymmetry.

- **Sampling.** Every claim is an assertion tied to a time range: event
  content (`confirm_event`), visible places (`confirm_place`), dated years
  (`confirm_event_date`), person presence (`confirm_identity`). Sampling is
  stratified by tape × claim type and deterministic per seed, oversampling
  low-confidence claims, chunk-boundary events, auto-accepted targets, and
  high-blast-radius places.
- **Blind protocol.** The verifier backend receives only a short local clip
  (ffmpeg, ≤120 s, 480p, under `verification_clips/`) plus the clip's raw
  speech with anonymized speaker labels, and returns a structured
  description — it never sees the claim, so it cannot be led. A separate
  adjudicator compares description to claim and issues
  `SUPPORTED` / `CONTRADICTED` / `UNDECIDABLE`. The offline baseline is
  keyword overlap; `--adjudicator llm` uses gpt-5.6-terra semantically (with
  keyword fallback on transport errors).
- **Live backend.** `TAPESPLIT_VERIFIER_BACKEND=azure` activates the
  gpt-5.6-sol backend. sol takes no video input, so a clip becomes ≤40
  sampled frames (360p JPEG, `detail=low`), each preceded by a
  `frame at Xs:` label — timestamps ride inside the request, so the
  verifier cannot introduce drift. Azure's content filter refuses some
  innocuous family footage; on `content_policy_violation` the frame batch
  is bisected, flagged frames dropped, and a fully-blocked clip degrades to
  a transcript-only description annotated `visual_blocked`.
- **Consequences.** Verdicts append to `verifications.jsonl`; contradictions
  are digested (never deleted) into `verification_flags.jsonl` for the review
  surface. In calibration, machine verdicts count for/against the claim's
  originating action type as reviewer `clip-verifier` at reduced weight
  (0.5× a human decision), and human-only precision is always reported
  alongside the blended figure.
- **Metrics.** `tapesplit verify report` computes grounded precision per
  claim type and per tape, with a first-class chunked-vs-whole-tape
  comparison — the instrument for proving fixes like the chunk-timestamp
  repair actually moved the needle.

The `verify` stage runs after `apply-suggestions` when a backend is
configured (`TAPESPLIT_VERIFIER_BACKEND=azure` plus Azure OpenAI env;
deployment via `TAPESPLIT_VERIFIER_DEPLOYMENT`, default `gpt-5.6-sol`) and is
cost-gated like the gemini stage. `tapesplit verify run --dry-run` prints the
sampled plan without extracting anything.

## Semantic search

`finalize` builds the keyword index (`search.sqlite`: FTS5 + sparse TF-IDF)
on every run; the `semantic-embed` stage (`src/tapesplit/semantic_search.py`)
then upgrades it with the layers that make search semantic:

- **Spoken/written meaning.** Dense vectors from a multilingual
  sentence-transformers model for the user-facing document types
  (transcripts, events, albums, people, places, captions, OCR). English
  queries hit Russian transcripts and vice versa. Vectors are cached by
  content hash in `semantic_cache.sqlite`, so re-finalizes only encode text
  that changed — the first haul pass costs minutes, later passes seconds.
- **What's on screen.** Scene-keyframe CLIP vectors (already produced by
  `visual-embed`) are copied into a `clip_vectors` table and queried with the
  CLIP text tower: "birthday cake" or "snow" finds footage nobody transcribed
  or captioned. English-only (CLIP's text tower is not multilingual).
- **Sectioned results.** `tapesplit search semantic <project> "query"`
  returns People / Places / Moments / Spoken / Seen; `tapesplit search serve`
  is a warm JSON-lines sidecar (models loaded once) that the review UI's ⌘K
  overlay talks to via `/api/search/semantic` — interactive queries answer in
  ~100–300 ms instead of paying model load per query. Without the upgrade the
  same endpoints fall back to keyword matching and say so.

## Claim substrate (v2 M1)

The v2 re-foundation (`docs/REFOUNDATION.md`) replaces stored conclusions
with claims — assertions carrying producer provenance, a span with an
explicit reference frame, confidence, and verification status. During the
strangler cutover v1 artifacts stay authoritative; producers *dual-write*
claims alongside them into `claim_store.sqlite3` in the project dir
(`src/tapesplit/claim_store.py`):

- gemini import, heuristic events, and review actions mirror every row (and
  every rejected candidate) as claims; regeneration supersedes prior claims
  rather than deleting them.
- Spans are `{clock, start_s, end_s}` with `clock ∈ {source, chunk,
  capture}`; a span violating its clock's bounds is rejected at write time,
  never clamped. Chunk→source conversion is an explicit function that
  reports the applied offset.
- Dual-write is best-effort and side-effect free for v1: with
  `TAPESPLIT_CLAIMS=0` (or on any store error) v1 outputs are byte-identical
  and the run summary records the reason.
- `media.jsonl` (media-agnostic source registry) is derived from
  `tapes.jsonl`; new code reads ids via `media.source_media_id()` so the
  `source_video_id` rename can land module-by-module.

`tapesplit claims stats|export|backfill` inspect and migrate the store;
`tapesplit claims diff --artifact gemini_events.jsonl` is the M1 oracle gate —
it proves the v1 artifact is regenerable from live claims byte-for-byte
(exit 1 on drift).

### Date model (v2 M2)

`src/tapesplit/date_model.py` gives every date reference an origin from a
fixed taxonomy (`overlay_datestamp`, `narrated_current`,
`narrated_historical`, `handwritten`, `era_estimate`, `age_anchor`,
`exif_capture`, `exif_scan`) and every medium a plausible **capture window**
(`capture_windows.jsonl`) fused from day-precision camcorder datestamp
overlays (trusted core), narrated full dates and bare on-screen years
(admitted only near the core — historical signage like "1900" on a plaque
cannot widen a window), and container/EXIF dates (stamps far from the core
classify as `exif_scan`, i.e. digitization dates, and are excluded). During
`build-groups`, each date group is classified against its media's window:
full dates outside the window become `narrated_historical` even when the
extractor missed them; in-window bare years stay excluded from chronology
but route to `resolve_date` review; unclassifiable clues carry
`origin: null`, which the schema contract only permits on review-flagged
groups. Both artifacts dual-write date claims, and
`tapesplit claims diff --artifact date_groups.jsonl|capture_windows.jsonl`
extends the oracle gate to the date model.

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

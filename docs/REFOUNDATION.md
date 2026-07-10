# TapeSplit v2: Semantic-Core Re-foundation

Design for re-founding TapeSplit's semantic core — the data model where
entities, evidence, contexts, dates, and events live — while keeping the
validated periphery (orchestrator, adapters, ML stages, UI shell, calibration)
untouched. Written for review before any implementation starts. Companion
docs: `AUTOMATION.md` (stage graph, verification loop), `HANDOFF.md` (archive
snapshot).

Status of inputs: grounded in the July 2026 research fleet — state audit,
chunk-timestamp root cause, Apple-UX gap analysis, gpt-5.6 integration probes,
and the scanned-photo track design — all validated against the 19-tape /
31.4h `examples/family-haul.tapesplit` archive.

## 1. Why re-found the semantic core — and only it

Every user-reported failure of the last review cycle traces to the same
layer: assertions that lost their origin, their reference frame, or their
qualifiers somewhere between the model response and the screen.

| Failure | Mechanism | Evidence |
|---|---|---|
| #12 context conflation | One context entity serves Moscow-Russia and Eugene-Oregon; contexts are tape-scoped strings, not typed objects | `place_context_8bfd7d073be9` attached to both Средняя школа №123 and Maplewood Learning Community at 0.9 conf; ~25 places under impossible compound scopes ("Moscow, Oregon", "Alaska, Davos, Switzerland") |
| #12 cemented errors | 110 auto-accepted `confirm_place_context` corrections replay on every rebuild; 232 more queued | `corrections.jsonl`: 181/181 rows `reviewer=auto-pipeline` |
| #13 timestamp corruption | Chunk prompts announced the full tape's duration (fixed: `gemini_adapter.py:286` now passes `chunk.duration_s`); importer clamped end but kept drifted start and silently dropped 43 events (`gemini_import.py:261-302`) | 75/318 raw chunk events overran their chunk; frame audit: 26% visual mismatch on chunked tapes, 48% on video_000018 |
| #13 invisibility | Within-chunk drift passes every timestamp validation; only content-aware checks catch it | video_000018 chunk_0001: "Night Vision House Tour" frame shows a sunflower field, zero validation notes |
| #14 flag dropping | `date_groups.jsonl` correctly flagged 1912/1974 as `excluded_as_event_date`; `_date_ref` dropped the flag; UI grabbed the first year (fixed at the presentation layer in d12f8f4) | 5 events sorted under 1870/1912/1974/1989 |
| Scale/debt | `evidence.jsonl` is 27MB for 19 tapes; `source_video_id` is the universal join key in 677 call sites across 39 modules, blocking the photo track | `wc`/grep counts, July 2026 |

The pattern: v1 stores *conclusions* ("this place has Moscow context",
"this event is at 7043s") without the metadata needed to re-derive, audit, or
scope them. Each bug above was fixable only by a human noticing a wrong pixel.
The v2 core stores *claims* — assertions that carry provenance, reference
frame, confidence, and verification status — so wrongness of these classes is
either structurally impossible or mechanically detectable.

### Prior art already in v1

The July 4 session (55597c2..31d5a89) began evolving v1 toward exactly this
design, in bolt-on form. v2's job is to make these mechanisms first-class in
the data model rather than post-hoc passes:

- **e94c132** — geographic containment vs era inheritance split in place
  scope labels (`grouping.py:1512`, `:1658`): direct admin evidence wins,
  lone inherited labels read as eras ("Madison era"), conflicts fall back to
  honest year scopes and flag review (54 conflicts surfaced on the haul),
  named institutions never inherit era labels.
- **31d5a89** — `regrounding.py`: CLIP text-vs-scene-keyframe search that
  detects mislocated events and proposes `move_event_range` actions — v1's
  organic answer to content-aware drift detection.
- **2037bf0 / 450f72f / d1f18ba** — transcript-support ranking
  (`event_alignment.py:444`) flags events whose descriptions the transcript
  does not support; visibility integration in 33b09a3.
- **d12f8f4** — `excluded_as_event_date` propagated through `_date_ref`
  (`visualization.py:1657`) to the UI year grouping.
- **3e5f47f** — the blind clip-verification stage (`verification.py`) with
  calibration weighting.

Each is kept (see §4–6); what changes is where the knowledge lives — in
typed claims validated at write time, not in string composition and flags
applied at display time.

**Carry-over list (validated, not rewritten):**

- `auto.py` stage-graph orchestrator (resume, coverage, cost gates)
- `gemini_adapter.py` and its empirically-discovered Vertex limits
  (2h/900k-token whole-video ceiling, chunk routing, retry/backoff)
- whisper transcription, faces/ArcFace, CLIP embeddings, OCR, captions stages
- review UI shell (photos-shell, sheets, drawer, design system)
- calibration loop (`calibration.py`) and the new blind clip-verification
  stage (`verification.py`) — v2 makes them load-bearing, not different

## 2. The claim model

One substrate for every assertion the pipeline makes. Sketch:

```json
{
  "id": "claim_01J...",
  "kind": "event | place_link | date | identity | relationship | presence",
  "subject": {
    "type": "media_segment",
    "media_id": "media_000018",
    "span": {"clock": "source", "start_s": 6735.0, "end_s": 6895.0}
  },
  "assertion": { "...kind-specific payload (title/summary, place ref, date value, person ref)" },
  "provenance": {
    "producer": "gemini-2.5-flash | gpt-5.6-sol | whisper | human | heuristic",
    "run_id": "run_20260713T...",
    "request_scope": {"kind": "chunk", "chunk_id": "chunk_0008", "chunk_offset_s": 6195.0},
    "derived_from": ["claim_...", "claim_..."]
  },
  "confidence": 0.82,
  "verification": {
    "status": "unverified | supported | contradicted | undecidable",
    "votes": [{"verifier": "clip-verifier/gpt-5.6-terra", "verdict": "supported", "at": "..."}]
  },
  "superseded_by": null
}
```

Properties that kill the observed bug classes:

- **Timestamps carry their reference frame.** A span is `{clock, start_s,
  end_s}` where `clock ∈ {source, chunk, capture}`. Conversions are explicit
  functions that record the applied offset in provenance. A chunk-clock span
  whose end exceeds its chunk's duration is a *validation error at the import
  boundary* — not a silent clamp (`gemini_import.py:282-288`'s clamp/drop
  behavior becomes unrepresentable). The #13 class cannot be stored, only
  rejected or rescaled with an audit note.
- **Nothing is deleted, only superseded.** The importer's silent drop of 43
  events becomes a recorded rejection claim a rebuild report can surface.
- **Human decisions are claims too** — `producer: human` with the review
  action in the assertion. `corrections.jsonl` remains the append-only intake
  log; replay becomes "re-assert human claims", and the 9 event-id-remap
  fragility disappears because human claims bind to media spans, not to
  renumbered canonical ids.
- **Verification status is a first-class field**, written by the blind
  clip-verification stage and cross-model votes — the UI and auto-accept
  gates read it directly.

Derived artifacts (`canonical_events.jsonl`, `visualization.json`, search
index) remain — they become *views* compiled from claims, always
regenerable, never hand-mutated.

## 3. Media-agnostic sources

Adopted from the photo-track design (fleet report):

- `media.jsonl` supersedes `tapes.jsonl`: `{media_id, media_type: "tape" |
  "photo" | "audio", path, probe, capture_window}`. A photo is a media row
  with no duration; a scanned album page is a parent media row with child
  crop rows.
- `source_video_id` → `source_media_id` with a read-compat shim during
  cutover (677 usages across 39 modules make big-bang rename risky; the shim
  makes it mechanical).
- Events gain `temporal_basis: "source_clock" | "capture_date"` — tape
  events order by tape time, photo events by (estimated) capture date. The
  stitcher's tape-time adjacency scoring applies only to `source_clock`
  events; photo events group by date ∩ similarity ∩ face co-occurrence ∩
  scan adjacency.
- The vision stack (faces `faces.py:50-71`, OCR, captions, CLIP
  `visual_embeddings.py:24-40`) already consumes image files from
  `visual_assets.jsonl` and needs only the subject-whitelist touchpoints
  (`visual_assets.py:297-312` and siblings) to accept `photo` subjects.

## 4. Context model v2

v1 flattens two different ideas into one display string. v2 splits them:

- **GeoContext** — physical containment: venue → city → region → country,
  gazetteer-backed, existing only for places with resolved geography.
  "Maplewood Learning Community ⊂ Eugene ⊂ Oregon ⊂ USA."
- **EraContext** — a period of family life: `{label: "Moscow, Idaho years",
  window: [1998, 2003-08], residence: place_ref}`. Eras attach to *segments*,
  never to tapes: continuity carries an era across a tape until a break
  signal — non-content gap, language shift, date-overlay jump, or a
  named-place anchor — and one tape can span the Moscow→Oregon move.
- **Named-institution anchors.** A high-confidence institution resolution
  (Maplewood → Eugene) is a hard anchor: it vetoes inherited era context for
  its segment and seeds a break. Anchor table starts from OCR'd signage +
  transcript proper nouns + human confirmations.
- **Contradiction guard.** A claim linking a place to a geographic parent
  disjoint from its anchored parent (Moscow-Russia school under an Oregon
  era) is never auto-acceptable; it becomes a review item carrying both
  hypotheses. This gate lives in the claim validator, upstream of
  `review_actions.py` floors.
- **Residence-by-era memory.** Generic places ("home", "school") resolve
  *through the era*: "Home · Moscow, Idaho" pre-move, "Home · Eugene,
  Oregon" after. The audit found zero geographic options on all 9 home/house
  groups — this is new inference, not display polish.
- **Remediation of cemented errors.** All 110 `confirm_place_context`
  corrections are machine-made (`reviewer=auto-pipeline`); nothing human is
  lost by reverting. Plan: mark all 110 superseded, re-derive contexts under
  v2, re-grade the 232 backlog items against the new model, and route
  survivors through verification votes before any re-acceptance.

**Relationship to e94c132.** That commit is the display-layer forerunner of
this model, and its *semantics* carry over verbatim: direct geographic
evidence outranks inheritance, a lone inherited label presents as an era, a
named institution never inherits a residence label, and conflicting
inheritance asserts nothing. What v2 replaces is the *mechanism*: e94c132
composes label strings and flags conflicts at render time
(`grouping.py:1512`, `scope_conflict` metadata), so the same conflation can
still be stored, auto-accepted, and cemented upstream — it just displays
honestly. In v2 the split exists at write time as typed GeoContext/EraContext
claims, the contradiction guard blocks acceptance rather than annotating
display, and the 54 scope conflicts e94c132 surfaced become the first
remediation worklist.

## 5. Date model v2

Every date reference carries an origin taxonomy:

```
origin: overlay_datestamp | narrated_current | narrated_historical |
        handwritten | era_estimate | age_anchor | exif_capture | exif_scan
```

- **Per-media plausible capture window**, derived from overlay datestamps,
  EXIF, and corroborated neighbor events. A date far outside the window
  (1912 on a 2005–2006 tape) is auto-classified `narrated_historical` even
  when the extractor missed it; a date that can't be classified becomes a
  `resolve_date` review item rather than a chronology bucket.
- **Origins survive every layer.** d12f8f4 already delivers the exclusion
  bit end-to-end (`visualization.py:1657` → UI year grouping) — the narrow
  fix for #14's visible symptom. What remains for v2: the full origin
  taxonomy (an excluded date should say *why* — narrated-historical vs
  unresolved), per-media capture windows that auto-classify what the
  extractor missed, `resolve_date` routing for ambiguity, and age anchors.
  v2 makes the origin enum part of the DateRef schema contract so no layer
  can drop qualifiers without failing validation.
- **Photo-side origins** (`handwritten`, `era_estimate`, `age_anchor`,
  `exif_scan`) slot into the same enum — the scanner-EXIF trap (scan date ≠
  capture date) is handled by origin, not by special cases. Age anchoring
  (dated tape appearances of a face bound undated photos of the same
  cluster) emits `age_anchor` DateRefs at conf ≤ 0.6.

## 6. Verification-native

The blind clip-verification loop (`verification.py`, stage `verify` — see
AUTOMATION.md) is a peer of generation in v2, not an add-on:

- **Grounded precision is the cutover gate.** Per claim type per tape,
  measured by blind describe-then-adjudicate sampling. A v2 module ships
  only when its grounded precision meets or beats v1's on the same archive.
- **Content-aware checks cover the invisible class.** Within-chunk drift
  passes every timestamp validation; only comparing footage to claim catches
  it. v1 already grew two such checks organically, and both stay as
  detection/repair layers feeding the same instrumentation:
  `regrounding.py` (31d5a89) — CLIP search over scene keyframes proposing
  `move_event_range` for mislocated events — and transcript-support
  flagging (`event_alignment.py:444`, 2037bf0/450f72f/d1f18ba) — marking
  events whose descriptions the audio does not support. In v2 their outputs
  are votes on the affected claims (visual-similarity and cross-modal
  respectively), their detections raise those claims' sampling priority for
  blind clip verification, and their repairs are provenance-carrying claims
  rather than in-place mutations. Sampling policy oversamples chunk
  boundaries, auto-accepted claims, regrounding/transcript-flagged claims,
  and high-blast-radius claims.
- **Cross-model votes feed calibration.** Machine verdicts enter
  `calibration.py` as the `clip-verifier` reviewer at reduced weight
  (machine 0.5× vs human 1× vs human-override 2×); human-only precision is
  reported separately so machine votes can never mask human signal.
- **Contradicted ≠ deleted.** A contradicted claim is downgraded and
  surfaced; its auto-accept (if any) is reverted with an audit trail.

## 7. Strangler execution plan

v1 keeps running throughout as the reference oracle. Dual-write first,
diff continuously, cut over per module, retire v1 artifacts last.

| # | Module | What happens | Gate | Effort |
|---|---|---|---|---|
| M1 | Claim substrate | Storage + schema + validators; importers dual-write claims alongside v1 artifacts; `media.jsonl` + `source_media_id` shim | Round-trip: v1 artifacts regenerable from claims byte-for-byte for one full rebuild | 3–4 d |
| M2 | Date model | Origin enum, capture windows, DateRef contract to UI | Zero impossible years in chrono view; grounded precision on date claims ≥ v1 | 1–2 d |
| M3 | Context model | GeoContext/EraContext split, segment continuity, anchors, contradiction guard, 110-correction remediation | Fleet's named conflations resolve correctly (Moscow-Russia vs Idaho vs Eugene vs Wisconsin); place-claim grounded precision ≥ v1 | 3–5 d |
| M4 | Event assembly | Stitcher consumes claims; temporal_basis-aware grouping | Event diff vs v1 reviewed; event grounded precision ≥ v1 (chunked tapes must exceed v1's 74%) | 2–3 d |
| M5 | Evidence compaction | 27MB evidence.jsonl → indexed claim store; graph edges reference claims | Rebuild wall-time and artifact size drop; no consumer regression | 1–2 d |
| M6 | Photo track Phase 0 | Photo ingest lands directly on v2 substrate (no v1 debt) | Photos in Albums/People with shared face graph | 3–4 d |
| M7 | Retirement | v1 semantic artifacts become build outputs only; remove dual-write | N consecutive green dual-run diffs | 1 d |

Ordering rationale: M1 is enabling; M2 is smallest-risk proof of the
pattern; M3 is the flagship (and the actual #12 fix); M4 depends on clean
dates/contexts; M6 deliberately follows M1 so photos never touch v1 debt.

Parallel with the Azure calendar (credits die 2026-07-22): the terra
claim-packet verification runs and the sol second-opinion event layer
generate exactly the cross-model claim corpus M1's substrate is designed to
hold, and the verification baselines M2–M4's gates need. Run them on v1 now;
their outputs migrate as claims.

## 8. Open decisions

1. **Claim storage: JSONL-per-kind vs SQLite.** Recommendation: SQLite for
   the claim store (27MB+ of evidence wants indexes; joins dominate), with
   JSONL export retained for portability and diffing. The project dir stays
   self-contained either way.
2. **`source_media_id` rename timing.** Recommendation: at M1 via
   read-compat shim; mechanical rename lands module-by-module behind it.
   Deferring past photo-track Phase 0 makes the debt permanent.
3. **Remediation mode for the 110 cemented place-context corrections.**
   Recommendation: revert all and re-derive under v2 (they are 100%
   machine-made; re-grading each individually spends effort to preserve
   nothing human).
4. **Geocoding source for GeoContext.** Recommendation: one-time cloud
   geocode of the 141 named place candidates now (also unblocks the Map
   view), cached into the project; offline gazetteer as fallback. Keeps
   local-first: geocodes are data, regenerable.
5. **Event identity across the cutover.** M4 re-assembly will renumber and
   re-shape events (v1's 311 include chunk-corrupted ranges). Recommendation:
   accept one reviewed diff rather than contorting to preserve ids — human
   claims bind to media spans, so nothing breaks.
6. **Start timing.** Recommendation: start M1 now, parallel to the Azure
   experiment calendar — dual-write is additive and the cloud runs produce
   the richest corpus for it. Waiting until after July 22 wastes the corpus.

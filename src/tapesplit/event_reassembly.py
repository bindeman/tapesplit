"""Second-opinion event re-assembly (v2 M4).

Reconciles the gemini source-event layer against the azure second-opinion
adjudication and CLIP regrounding proposals before stitching. Nothing edits
gemini_events.jsonl in place: the outputs are provenance-carrying artifacts
that load_source_events folds in at read time, so a rebuild from raw
artifacts reproduces the same reconciliation deterministically.

Outputs (project dir):
- source_event_adjustments.jsonl — per-source-event range corrections and
  content-dispute review flags, each with basis + provenance.
- azure_events.jsonl — azure-only event candidates promoted into the
  analyzed source-event layer (gemini-event shaped, provider-tagged).
- event_promotion_backlog.jsonl — candidates below the promotion floor,
  kept for the review queue instead of being silently dropped.

Reconciliation weights are measured, not assumed: tapes whose event claims
scored badly in blind clip verification (verifications.jsonl) accept
regrounding proposals at a relaxed margin, because a measured 8%-precision
source has no standing against a frame-anchored counter-proposal.
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from tapesplit.claim_store import DualWriter
from tapesplit.storage import append_jsonl, read_jsonl


ADJUDICATION_FILENAME = "azure_adjudication.json"
ADJUSTMENTS_FILENAME = "source_event_adjustments.jsonl"
AZURE_EVENTS_FILENAME = "azure_events.jsonl"
PROMOTION_BACKLOG_FILENAME = "event_promotion_backlog.jsonl"

# Promotion policy for azure-only candidates (all pre-filtered likely_family).
PROMOTE_CONFIDENCE = 0.9
PROMOTE_REVIEW_CONFIDENCE = 0.7
# An azure-only candidate overlapping an existing canonical range this much
# is a duplicate sighting of known content, not a missed event.
OVERLAP_DEDUP_RATIO = 0.5

# CLIP regrounding proposals carry no transcript corroboration on this
# archive, so they only auto-apply with a strong score margin — unless the
# tape's own gemini ranges measured near-random in blind verification.
REGROUND_AUTO_MARGIN = 0.15
REGROUND_LOW_PRECISION_MARGIN = 0.08
LOW_PRECISION_MAX = 0.45
LOW_PRECISION_MIN_SAMPLES = 8

_EVENT_CLAIM_TYPES = {"event_content", "event_place", "event_date"}


def measure_tape_event_precision(project: Path) -> dict[str, dict[str, Any]]:
    """Grounded precision of event claims per tape, from blind verification."""

    tallies: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in read_jsonl(project / "verifications.jsonl"):
        if row.get("claim_type") not in _EVENT_CLAIM_TYPES:
            continue
        verdict = row.get("verdict")
        if verdict not in ("SUPPORTED", "CONTRADICTED"):
            continue
        tally = tallies[str(row.get("source_video_id"))]
        tally[1] += 1
        if verdict == "SUPPORTED":
            tally[0] += 1
    return {
        source: {"precision": supported / total, "samples": total}
        for source, (supported, total) in tallies.items()
        if total > 0
    }


def low_precision_tapes(project: Path) -> set[str]:
    return {
        source
        for source, stats in measure_tape_event_precision(project).items()
        if stats["samples"] >= LOW_PRECISION_MIN_SAMPLES and stats["precision"] < LOW_PRECISION_MAX
    }


def build_event_reassembly(project_dir: Path) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    adjudication_path = project / ADJUDICATION_FILENAME
    summary: dict[str, Any] = {"project": str(project)}
    if not adjudication_path.exists():
        summary["skipped"] = "no azure_adjudication.json"
        return summary

    adjudication = json.loads(adjudication_path.read_text(encoding="utf-8"))
    adjudicated_at = str(adjudication.get("created_at") or "")
    verdicts = adjudication.get("verdicts") or {}
    canonical = {row.get("id"): row for row in read_jsonl(project / "canonical_events.jsonl")}
    gemini_ids = {row.get("id") for row in read_jsonl(project / "gemini_events.jsonl")}
    weak_tapes = low_precision_tapes(project)

    for name in (ADJUSTMENTS_FILENAME, AZURE_EVENTS_FILENAME, PROMOTION_BACKLOG_FILENAME):
        path = project / name
        if path.exists():
            path.unlink()

    dual = DualWriter.open(
        project,
        artifact=AZURE_EVENTS_FILENAME,
        producer="event-reassembly/azure-second-opinion",
        run_id=adjudicated_at or None,
    )
    dual.supersede_previous()

    adjustments: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    adjusted_source_ids: set[str] = set()

    def source_event_for(canonical_event_id: str) -> str | None:
        row = canonical.get(canonical_event_id)
        if not row:
            return None
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        source_ids = [sid for sid in metadata.get("source_event_ids") or [] if sid in gemini_ids]
        if len(source_ids) == 1:
            return source_ids[0]
        return None

    # 1. Azure range disputes: frame-anchored counter-ranges win outright.
    for dispute in verdicts.get("range_disputed") or []:
        canonical_id = dispute.get("canonical_event_id")
        source_id = source_event_for(canonical_id)
        azure_range = dispute.get("azure_range") or []
        if source_id is None or len(azure_range) != 2:
            skipped.append({"canonical_event_id": canonical_id, "reason": "unmappable range dispute"})
            continue
        adjustments.append(
            {
                "source_event_id": source_id,
                "canonical_event_id": canonical_id,
                "source_video_id": dispute.get("source_video_id"),
                "basis": "azure_range_dispute",
                "old": {"start_s": dispute.get("start_s"), "end_s": dispute.get("end_s")},
                "new": {"start_s": float(azure_range[0]), "end_s": float(azure_range[1])},
                "needs_review": False,
                "detail": f"second opinion places '{dispute.get('azure_title')}' at this range",
                "adjudicated_at": adjudicated_at,
            }
        )
        adjusted_source_ids.add(source_id)

    # 2. Content disputes: no range change, honest review flag with the
    #    competing description attached.
    for dispute in verdicts.get("content_disputed") or []:
        canonical_id = dispute.get("canonical_event_id")
        source_id = source_event_for(canonical_id)
        if source_id is None:
            skipped.append({"canonical_event_id": canonical_id, "reason": "unmappable content dispute"})
            continue
        adjustments.append(
            {
                "source_event_id": source_id,
                "canonical_event_id": canonical_id,
                "source_video_id": dispute.get("source_video_id"),
                "basis": "azure_content_dispute",
                "old": {"start_s": dispute.get("start_s"), "end_s": dispute.get("end_s")},
                "new": None,
                "needs_review": True,
                "azure_title": dispute.get("azure_title"),
                "detail": "second opinion describes different content in this range",
                "adjudicated_at": adjudicated_at,
            }
        )
        adjusted_source_ids.add(source_id)

    # 3. CLIP regrounding proposals: azure verdicts outrank them; the rest
    #    auto-apply only with a strong margin (relaxed on measured-weak tapes).
    reground_applied = 0
    reground_deferred = 0
    for proposal in read_jsonl(project / "event_regroundings.jsonl"):
        canonical_id = proposal.get("canonical_event_id")
        source_id = source_event_for(canonical_id)
        if source_id is None or source_id in adjusted_source_ids:
            reground_deferred += 1
            continue
        margin = float(proposal.get("margin") or 0.0)
        tape = str(proposal.get("source_video_id"))
        threshold = REGROUND_LOW_PRECISION_MARGIN if tape in weak_tapes else REGROUND_AUTO_MARGIN
        if margin < threshold:
            reground_deferred += 1
            continue
        adjustments.append(
            {
                "source_event_id": source_id,
                "canonical_event_id": canonical_id,
                "source_video_id": tape,
                "basis": "regrounding",
                "old": {"start_s": proposal.get("current_start_s"), "end_s": proposal.get("current_end_s")},
                "new": {
                    "start_s": float(proposal.get("proposed_start_s")),
                    "end_s": float(proposal.get("proposed_end_s")),
                },
                "needs_review": tape not in weak_tapes,
                "detail": (
                    f"CLIP regrounding margin {margin:.3f} ≥ {threshold} "
                    f"({'measured-weak tape' if tape in weak_tapes else 'strong margin'})"
                ),
                "adjudicated_at": adjudicated_at,
            }
        )
        adjusted_source_ids.add(source_id)
        reground_applied += 1

    for row in adjustments:
        append_jsonl(project / ADJUSTMENTS_FILENAME, row)
        new_range = row.get("new") or {}
        dual.write_row(
            row,
            kind="event",
            media_id=row.get("source_video_id"),
            start_s=new_range.get("start_s"),
            end_s=new_range.get("end_s"),
            confidence=None,
            assertion={"adjustment": row["basis"], "canonical_event_id": row["canonical_event_id"]},
        )

    # 4. Azure-only candidates: promote what the canonical layer missed.
    promoted = 0
    promoted_review = 0
    backlogged = 0
    deduped = 0
    ranges_by_tape: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in canonical.values():
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        for source_range in metadata.get("source_ranges") or []:
            tape = str(source_range.get("source_video_id"))
            start = source_range.get("start_s")
            end = source_range.get("end_s")
            if start is None or end is None:
                continue
            ranges_by_tape[tape].append((float(start), float(end)))

    candidates = sorted(
        adjudication.get("azure_only") or [],
        key=lambda row: (str(row.get("source_video_id")), float(row.get("start_s") or 0.0), str(row.get("title"))),
    )
    for index, candidate in enumerate(candidates, start=1):
        confidence = float(candidate.get("confidence") or 0.0)
        tape = str(candidate.get("source_video_id"))
        start = float(candidate.get("start_s") or 0.0)
        end = float(candidate.get("end_s") or 0.0)
        if _overlaps_existing(start, end, ranges_by_tape.get(tape) or []):
            deduped += 1
            continue
        if confidence < PROMOTE_REVIEW_CONFIDENCE:
            backlogged += 1
            append_jsonl(
                project / PROMOTION_BACKLOG_FILENAME,
                {**candidate, "reason": f"confidence {confidence} below promotion floor"},
            )
            continue
        needs_review = confidence < PROMOTE_CONFIDENCE
        event_row = {
            "id": f"az_event_{index:06d}",
            "title": candidate.get("title"),
            "summary": candidate.get("title"),
            "start_s": start,
            "end_s": end,
            "confidence": confidence,
            "relatedness": candidate.get("relatedness") or "likely_family",
            "review_status": "needs_review" if needs_review else "unreviewed",
            "source": "azure_second_opinion",
            "source_video_id": tape,
            "evidence_ids": [],
            "metadata": {
                "source_video_id": tape,
                "time_basis": "source_video",
                "promotion_basis": "azure_second_opinion",
                "adjudicated_at": adjudicated_at,
            },
        }
        append_jsonl(project / AZURE_EVENTS_FILENAME, event_row)
        dual.write_row(
            event_row,
            kind="event",
            media_id=tape,
            start_s=start,
            end_s=end,
            confidence=confidence,
            assertion={"title": candidate.get("title"), "promotion": "azure_only"},
        )
        promoted += 1
        if needs_review:
            promoted_review += 1

    summary.update(
        {
            "range_corrections": sum(1 for row in adjustments if row["basis"] == "azure_range_dispute"),
            "content_disputes_flagged": sum(1 for row in adjustments if row["basis"] == "azure_content_dispute"),
            "regrounding_applied": reground_applied,
            "regrounding_deferred": reground_deferred,
            "promoted": promoted,
            "promoted_needs_review": promoted_review,
            "promotion_backlog": backlogged,
            "deduped_against_canonical": deduped,
            "unmappable": skipped,
            "measured_weak_tapes": sorted(weak_tapes),
            "claims_written": dual.claims_written,
            "claim_store_error": dual.error,
        }
    )
    return summary


def load_reassembly_layers(project: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Adjustments keyed by source event id, plus promoted azure events."""

    adjustments = {
        str(row.get("source_event_id")): row for row in read_jsonl(project / ADJUSTMENTS_FILENAME)
    }
    azure_events = read_jsonl(project / AZURE_EVENTS_FILENAME)
    return adjustments, azure_events


def apply_adjustment(event: dict[str, Any], adjustment: dict[str, Any]) -> dict[str, Any]:
    adjusted = dict(event)
    metadata = dict(event.get("metadata") or {})
    notes = list(metadata.get("validation_notes") or [])
    new_range = adjustment.get("new")
    if new_range:
        adjusted["start_s"] = new_range.get("start_s")
        adjusted["end_s"] = new_range.get("end_s")
        notes.append(f"range_corrected:{adjustment.get('basis')}")
    if adjustment.get("needs_review"):
        adjusted["review_status"] = "needs_review"
        notes.append(f"disputed:{adjustment.get('basis')}")
        if adjustment.get("azure_title"):
            metadata["second_opinion_title"] = adjustment["azure_title"]
    metadata["validation_notes"] = notes
    metadata["reassembly_basis"] = adjustment.get("basis")
    adjusted["metadata"] = metadata
    return adjusted


def _overlaps_existing(start: float, end: float, ranges: list[tuple[float, float]]) -> bool:
    duration = max(0.0, end - start)
    if duration <= 0:
        return True
    for existing_start, existing_end in ranges:
        overlap = min(end, existing_end) - max(start, existing_start)
        if overlap <= 0:
            continue
        existing_duration = max(existing_end - existing_start, 0.001)
        if overlap / duration >= OVERLAP_DEDUP_RATIO or overlap / existing_duration >= OVERLAP_DEDUP_RATIO:
            return True
    return False


def remap_event_corrections(
    project_dir: Path,
    *,
    before_events: list[dict[str, Any]],
    after_events: list[dict[str, Any]],
    min_overlap_ratio: float = 0.5,
) -> dict[str, Any]:
    """Rewrite event-targeted correction ids after renumbering re-assembly.

    Matching is anchored the way human intent is: same tape, overlapping
    source range, then title similarity as the tie-breaker. Unmatched
    corrections are left untouched and reported (replay skips them
    non-strictly, and they surface for one-click re-confirmation).
    """

    from difflib import SequenceMatcher

    project = project_dir.expanduser().resolve()
    corrections_path = project / "corrections.jsonl"
    corrections = read_jsonl(corrections_path)
    if not corrections:
        return {"remapped": 0, "unmatched": []}

    before_by_id = {row.get("id"): row for row in before_events}

    def ranges_of(row: dict[str, Any]) -> list[tuple[str, float, float]]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        out = []
        for source_range in metadata.get("source_ranges") or []:
            tape = str(source_range.get("source_video_id"))
            start = source_range.get("start_s")
            end = source_range.get("end_s")
            if start is not None and end is not None:
                out.append((tape, float(start), float(end)))
        return out

    def best_match(old_row: dict[str, Any]) -> tuple[str | None, float]:
        old_ranges = ranges_of(old_row)
        old_title = str(old_row.get("title") or "")
        best_id, best_score = None, 0.0
        for candidate in after_events:
            overlap_ratio = 0.0
            for tape, start, end in old_ranges:
                duration = max(end - start, 0.001)
                for c_tape, c_start, c_end in ranges_of(candidate):
                    if c_tape != tape:
                        continue
                    overlap = min(end, c_end) - max(start, c_start)
                    if overlap > 0:
                        overlap_ratio = max(overlap_ratio, overlap / duration)
            if overlap_ratio < min_overlap_ratio:
                continue
            title_score = SequenceMatcher(
                None, old_title.lower(), str(candidate.get("title") or "").lower()
            ).ratio()
            score = overlap_ratio + title_score
            if score > best_score:
                best_id, best_score = candidate.get("id"), score
        return best_id, best_score

    remapped = 0
    unmatched = []
    rewritten = []
    for correction in corrections:
        target_id = correction.get("target_id")
        if correction.get("target_type") != "canonical_event" and not str(target_id or "").startswith(
            "canonical_event_"
        ):
            rewritten.append(correction)
            continue
        old_row = before_by_id.get(target_id)
        if old_row is None:
            rewritten.append(correction)
            unmatched.append({"correction_id": correction.get("id"), "target_id": target_id, "reason": "no snapshot row"})
            continue
        new_id, score = best_match(old_row)
        if new_id is None:
            rewritten.append(correction)
            unmatched.append({"correction_id": correction.get("id"), "target_id": target_id, "reason": "no overlap match"})
            continue
        updated = dict(correction)
        updated["target_id"] = new_id
        metadata = dict(updated.get("metadata") or {})
        metadata["remapped_from"] = target_id
        metadata["remap_score"] = round(score, 3)
        updated["metadata"] = metadata
        rewritten.append(updated)
        remapped += 1

    backup = corrections_path.with_suffix(".jsonl.pre_reassembly")
    if not backup.exists():
        corrections_path.rename(backup)
    else:
        corrections_path.unlink()
    for row in rewritten:
        append_jsonl(corrections_path, row)
    return {"remapped": remapped, "unmatched": unmatched, "backup": str(backup)}


def build_reassembly_diff(
    project_dir: Path,
    *,
    before_events: list[dict[str, Any]],
    after_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Skimmable before/after report for the reviewed-diff gate."""

    project = project_dir.expanduser().resolve()
    adjustments = read_jsonl(project / ADJUSTMENTS_FILENAME)
    azure_events = read_jsonl(project / AZURE_EVENTS_FILENAME)

    def by_tape(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            for tape in metadata.get("source_video_ids") or [metadata.get("source_video_id")]:
                if tape:
                    counts[str(tape)] += 1
                    break
        return dict(counts)

    before_counts = by_tape(before_events)
    after_counts = by_tape(after_events)
    promoted_titles = [
        {"title": row.get("title"), "tape": row.get("source_video_id"), "confidence": row.get("confidence")}
        for row in sorted(azure_events, key=lambda r: -float(r.get("confidence") or 0))
    ]

    report = {
        "before_total": len(before_events),
        "after_total": len(after_events),
        "per_tape": {
            tape: {"before": before_counts.get(tape, 0), "after": after_counts.get(tape, 0)}
            for tape in sorted(set(before_counts) | set(after_counts))
        },
        "range_corrections": [
            {
                "canonical_event_id": row.get("canonical_event_id"),
                "tape": row.get("source_video_id"),
                "old": row.get("old"),
                "new": row.get("new"),
                "basis": row.get("basis"),
            }
            for row in adjustments
            if row.get("new")
        ],
        "content_disputes": [
            {
                "canonical_event_id": row.get("canonical_event_id"),
                "tape": row.get("source_video_id"),
                "second_opinion_title": row.get("azure_title"),
            }
            for row in adjustments
            if row.get("basis") == "azure_content_dispute"
        ],
        "promoted_events": promoted_titles,
    }

    lines = [
        "# Event re-assembly diff (v2 M4)",
        "",
        f"Canonical events: **{len(before_events)} → {len(after_events)}**",
        "",
        f"- Range corrections applied: {len(report['range_corrections'])}",
        f"- Content disputes flagged for review: {len(report['content_disputes'])}",
        f"- Second-opinion events promoted: {len(promoted_titles)}",
        "",
        "## Per-tape event counts",
        "",
        "| tape | before | after |",
        "|---|---|---|",
    ]
    for tape, counts in report["per_tape"].items():
        lines.append(f"| {tape} | {counts['before']} | {counts['after']} |")
    lines += ["", "## Promoted events (top confidence first)", ""]
    for row in promoted_titles[:40]:
        lines.append(f"- {row['title']} ({row['tape']}, conf {row['confidence']})")
    if len(promoted_titles) > 40:
        lines.append(f"- … and {len(promoted_titles) - 40} more")
    (project / "event_reassembly_diff.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (project / "event_reassembly_diff.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return report

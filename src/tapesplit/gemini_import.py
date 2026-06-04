from __future__ import annotations

from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


def import_gemini_analysis(project_dir: Path) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    analyses = read_jsonl(project / "gemini_analyses.jsonl")
    if not analyses:
        raise FileNotFoundError(f"missing Gemini analysis file: {project / 'gemini_analyses.jsonl'}")
    latest = analyses[-1]
    analysis = latest.get("analysis") or {}
    source_video_id = latest.get("source_video_id")
    duration_s = _duration_for_source(project, source_video_id)
    hard_boundaries = _hard_non_content_boundaries(project, source_video_id)

    outputs = [
        project / "gemini_evidence.jsonl",
        project / "gemini_claims.jsonl",
        project / "gemini_events.jsonl",
    ]
    for path in outputs:
        if path.exists():
            path.unlink()

    evidence_count = 0
    claim_count = 0
    event_count = 0
    skipped = []

    def add_evidence(record: dict[str, Any]) -> str:
        nonlocal evidence_count
        evidence_count += 1
        evidence_id = f"gem_ev_{evidence_count:06d}"
        append_jsonl(
            project / "gemini_evidence.jsonl",
            {
                "id": evidence_id,
                "source": "gemini_vertex",
                "review_status": "unreviewed",
                "source_video_id": source_video_id,
                **record,
            },
        )
        return evidence_id

    def add_claim(record: dict[str, Any]) -> None:
        nonlocal claim_count
        claim_count += 1
        append_jsonl(
            project / "gemini_claims.jsonl",
            {
                "id": f"gem_claim_{claim_count:06d}",
                "source": "gemini_vertex",
                "review_status": record.pop("review_status", "unreviewed"),
                **record,
            },
        )

    summary_text = analysis.get("tape_summary")
    if summary_text:
        evidence_id = add_evidence(
            {
                "start_s": None,
                "end_s": None,
                "modality": "multimodal",
                "kind": "gemini_tape_summary",
                "text": str(summary_text),
                "confidence": 0.7,
                "metadata": {"model": latest.get("model"), "response_id": latest.get("response_id")},
            }
        )
        add_claim(
            {
                "subject": "tape",
                "predicate": "summary",
                "value": str(summary_text),
                "confidence": 0.7,
                "evidence_ids": [evidence_id],
            }
        )

    for item in analysis.get("event_candidates") or []:
        normalized = _normalize_interval(item, duration_s, hard_boundaries)
        if normalized is None:
            skipped.append({"kind": "event_candidate", "item": item})
            continue
        start_s, end_s, validation_notes = normalized
        evidence_id = add_evidence(
            {
                "start_s": start_s,
                "end_s": end_s,
                "modality": "multimodal",
                "kind": "gemini_event_candidate",
                "text": item.get("summary") or item.get("title") or "",
                "confidence": item.get("confidence", 0.6),
                "metadata": {
                    "title": item.get("title"),
                    "event_type": item.get("event_type"),
                    "people": item.get("people") or [],
                    "place_candidates": item.get("place_candidates") or [],
                    "date_candidates": item.get("date_candidates") or [],
                    "languages": item.get("languages") or [],
                    "relatedness": item.get("relatedness"),
                    "evidence_text": item.get("evidence_text") or [],
                    "validation_notes": validation_notes,
                    "needs_review": bool(item.get("needs_review")) or bool(validation_notes),
                },
            }
        )
        event_count += 1
        append_jsonl(
            project / "gemini_events.jsonl",
            {
                "id": f"gem_event_{event_count:06d}",
                "source": "gemini_vertex",
                "review_status": "needs_review" if validation_notes or item.get("needs_review") else "unreviewed",
                "title": item.get("title") or "Untitled Gemini event",
                "start_s": start_s,
                "end_s": end_s,
                "confidence": item.get("confidence", 0.6),
                "evidence_ids": [evidence_id],
                "summary": item.get("summary") or "",
                "relatedness": item.get("relatedness"),
                "metadata": {
                    "event_type": item.get("event_type"),
                    "people": item.get("people") or [],
                    "place_candidates": item.get("place_candidates") or [],
                    "date_candidates": item.get("date_candidates") or [],
                    "languages": item.get("languages") or [],
                    "validation_notes": validation_notes,
                },
            },
        )
        add_claim(
            {
                "subject": f"gem_event_{event_count:06d}",
                "predicate": "event",
                "value": item.get("title") or item.get("summary") or "Untitled Gemini event",
                "confidence": item.get("confidence", 0.6),
                "evidence_ids": [evidence_id],
                "review_status": "needs_review" if validation_notes or item.get("needs_review") else "unreviewed",
                "notes": "; ".join(validation_notes),
            }
        )

    for key, predicate in [
        ("person_mentions", "person_mention"),
        ("place_candidates", "place_candidate"),
        ("date_candidates", "date_candidate"),
    ]:
        for item in analysis.get(key) or []:
            normalized = _normalize_interval(item, duration_s, hard_boundaries)
            if normalized is None:
                skipped.append({"kind": key, "item": item})
                continue
            start_s, end_s, validation_notes = normalized
            value = item.get("name") or item.get("value") or item.get("evidence_text") or ""
            evidence_id = add_evidence(
                {
                    "start_s": start_s,
                    "end_s": end_s,
                    "modality": "multimodal",
                    "kind": f"gemini_{predicate}",
                    "text": item.get("evidence_text") or str(value),
                    "confidence": item.get("confidence", 0.6),
                    "metadata": {**item, "validation_notes": validation_notes},
                }
            )
            add_claim(
                {
                    "subject": "tape",
                    "predicate": predicate,
                    "value": value,
                    "confidence": item.get("confidence", 0.6),
                    "evidence_ids": [evidence_id],
                    "review_status": "needs_review" if validation_notes else "unreviewed",
                    "notes": "; ".join(validation_notes),
                }
            )

    return {
        "project": str(project),
        "source_video_id": source_video_id,
        "duration_s": duration_s,
        "evidence": evidence_count,
        "claims": claim_count,
        "events": event_count,
        "skipped": len(skipped),
    }


def _normalize_interval(
    item: dict[str, Any],
    duration_s: float,
    hard_boundaries: list[tuple[float, float, str]],
) -> tuple[float, float, list[str]] | None:
    start_s = _number_or_none(item.get("start_s"))
    end_s = _number_or_none(item.get("end_s"))
    if start_s is None and end_s is None:
        return 0.0, 0.0, ["missing_time_range"]
    if start_s is None:
        start_s = end_s
    if end_s is None:
        end_s = start_s
    if start_s is None or end_s is None:
        return None
    if end_s < start_s:
        start_s, end_s = end_s, start_s
    notes = []
    if start_s >= duration_s:
        return None
    if end_s > duration_s:
        end_s = duration_s
        notes.append("end_clamped_to_source_duration")

    for boundary_start, boundary_end, label in hard_boundaries:
        if start_s >= boundary_start and start_s < boundary_end:
            return None
        if start_s < boundary_start < end_s:
            end_s = boundary_start
            notes.append(f"end_clamped_before_{label}")
            break
    if end_s < start_s:
        return None
    return round(start_s, 3), round(end_s, 3), notes


def _duration_for_source(project: Path, source_video_id: str | None) -> float:
    for tape in read_jsonl(project / "tapes.jsonl"):
        if source_video_id is None or tape.get("id") == source_video_id:
            return float((tape.get("probe") or {}).get("duration_s") or 0.0)
    return 0.0


def _hard_non_content_boundaries(project: Path, source_video_id: str | None) -> list[tuple[float, float, str]]:
    boundaries = []
    for row in read_jsonl(project / "non_content_ranges.jsonl"):
        if source_video_id is not None and row.get("source_video_id") != source_video_id:
            continue
        label = str(row.get("label") or "non_content")
        duration = float(row.get("duration_s") or 0.0)
        if duration < 60 or "blue" not in label:
            continue
        start_s = _number_or_none(row.get("start_s"))
        end_s = _number_or_none(row.get("end_s"))
        if start_s is not None and end_s is not None:
            boundaries.append((start_s, end_s, label))
    return sorted(boundaries)


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

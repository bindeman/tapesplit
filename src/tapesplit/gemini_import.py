from __future__ import annotations

from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


def import_gemini_analysis(project_dir: Path, *, run_id: str | None = None, all_runs: bool = False) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    analyses = read_jsonl(project / "gemini_analyses.jsonl")
    if not analyses:
        raise FileNotFoundError(f"missing Gemini analysis file: {project / 'gemini_analyses.jsonl'}")
    if all_runs and run_id is not None:
        raise ValueError("run_id cannot be combined with all_runs")
    selected = _select_analyses(analyses, run_id, all_runs=all_runs)
    selected_run_id = "all" if all_runs else selected[-1].get("analysis_run_id")
    source_video_ids = _unique_string_items(row.get("source_video_id") for row in selected)

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

    def add_evidence(record: dict[str, Any], source_video_id: str | None) -> str:
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

    def add_claim(record: dict[str, Any], source_video_id: str | None) -> None:
        nonlocal claim_count
        claim_count += 1
        append_jsonl(
            project / "gemini_claims.jsonl",
            {
                "id": f"gem_claim_{claim_count:06d}",
                "source": "gemini_vertex",
                "review_status": record.pop("review_status", "unreviewed"),
                "source_video_id": source_video_id,
                **record,
            },
        )

    for latest in selected:
        analysis = latest.get("analysis") or {}
        source_video_id = latest.get("source_video_id")
        duration_s = _duration_for_source(project, source_video_id)
        hard_boundaries = _hard_non_content_boundaries(project, source_video_id)
        source_offset_s = _number_or_none(latest.get("chunk_start_s")) or 0.0
        chunk_end_s = _number_or_none(latest.get("chunk_end_s"))
        local_duration_s = None
        if chunk_end_s is not None:
            local_duration_s = max(0.0, chunk_end_s - source_offset_s)
        chunk_metadata = _chunk_metadata(latest)

        summary_text = analysis.get("tape_summary")
        if summary_text:
            evidence_id = add_evidence(
                {
                    "start_s": source_offset_s if latest.get("time_basis") == "chunk" else None,
                    "end_s": chunk_end_s if latest.get("time_basis") == "chunk" else None,
                    "modality": "multimodal",
                    "kind": "gemini_chunk_summary" if latest.get("time_basis") == "chunk" else "gemini_tape_summary",
                    "text": str(summary_text),
                    "confidence": 0.7,
                    "metadata": {
                        "model": latest.get("model"),
                        "response_id": latest.get("response_id"),
                        **chunk_metadata,
                    },
                },
                source_video_id,
            )
            add_claim(
                {
                    "subject": latest.get("chunk_id") or "tape",
                    "predicate": "summary",
                    "value": str(summary_text),
                    "confidence": 0.7,
                    "evidence_ids": [evidence_id],
                },
                source_video_id,
            )

        for item in analysis.get("event_candidates") or []:
            normalized = _normalize_interval(
                item,
                duration_s,
                hard_boundaries,
                source_offset_s=source_offset_s,
                local_duration_s=local_duration_s,
            )
            if normalized is None:
                skipped.append({"kind": "event_candidate", "item": item, "chunk": chunk_metadata})
                continue
            start_s, end_s, validation_notes = normalized
            local_start_s, local_end_s = _local_interval(item)
            event_metadata = {
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
                "chunk_local_start_s": local_start_s,
                "chunk_local_end_s": local_end_s,
                **chunk_metadata,
            }
            evidence_id = add_evidence(
                {
                    "start_s": start_s,
                    "end_s": end_s,
                    "modality": "multimodal",
                    "kind": "gemini_event_candidate",
                    "text": item.get("summary") or item.get("title") or "",
                    "confidence": item.get("confidence", 0.6),
                    "metadata": event_metadata,
                },
                source_video_id,
            )
            event_count += 1
            append_jsonl(
                project / "gemini_events.jsonl",
                {
                    "id": f"gem_event_{event_count:06d}",
                    "source": "gemini_vertex",
                    "source_video_id": source_video_id,
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
                        "source_video_id": source_video_id,
                        "people": item.get("people") or [],
                        "place_candidates": item.get("place_candidates") or [],
                        "date_candidates": item.get("date_candidates") or [],
                        "languages": item.get("languages") or [],
                        "validation_notes": validation_notes,
                        **chunk_metadata,
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
                },
                source_video_id,
            )

        for key, predicate in [
            ("person_mentions", "person_mention"),
            ("place_candidates", "place_candidate"),
            ("date_candidates", "date_candidate"),
        ]:
            for item in analysis.get(key) or []:
                normalized = _normalize_interval(
                    item,
                    duration_s,
                    hard_boundaries,
                    source_offset_s=source_offset_s,
                    local_duration_s=local_duration_s,
                )
                if normalized is None:
                    skipped.append({"kind": key, "item": item, "chunk": chunk_metadata})
                    continue
                start_s, end_s, validation_notes = normalized
                local_start_s, local_end_s = _local_interval(item)
                value = item.get("name") or item.get("value") or item.get("evidence_text") or ""
                evidence_id = add_evidence(
                    {
                        "start_s": start_s,
                        "end_s": end_s,
                        "modality": "multimodal",
                        "kind": f"gemini_{predicate}",
                        "text": item.get("evidence_text") or str(value),
                        "confidence": item.get("confidence", 0.6),
                        "metadata": {
                            **item,
                            "validation_notes": validation_notes,
                            "chunk_local_start_s": local_start_s,
                            "chunk_local_end_s": local_end_s,
                            **chunk_metadata,
                        },
                    },
                    source_video_id,
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
                    },
                    source_video_id,
                )

    return {
        "project": str(project),
        "source_video_id": source_video_ids[0] if len(source_video_ids) == 1 else None,
        "source_video_ids": source_video_ids,
        "analysis_run_id": selected_run_id,
        "analyses": len(selected),
        "evidence": evidence_count,
        "claims": claim_count,
        "events": event_count,
        "skipped": len(skipped),
    }


def _normalize_interval(
    item: dict[str, Any],
    duration_s: float,
    hard_boundaries: list[tuple[float, float, str]],
    *,
    source_offset_s: float = 0.0,
    local_duration_s: float | None = None,
) -> tuple[float, float, list[str]] | None:
    start_s = _number_or_none(item.get("start_s"))
    end_s = _number_or_none(item.get("end_s"))
    notes = []
    if start_s is None and end_s is None:
        return round(source_offset_s, 3), round(source_offset_s, 3), ["missing_time_range"]
    if start_s is None:
        start_s = end_s
    if end_s is None:
        end_s = start_s
    if start_s is None or end_s is None:
        return None
    if end_s < start_s:
        start_s, end_s = end_s, start_s
    if local_duration_s is not None and end_s > local_duration_s:
        end_s = local_duration_s
        notes.append("end_clamped_to_chunk_duration")
    start_s += source_offset_s
    end_s += source_offset_s
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


def _select_analyses(analyses: list[dict[str, Any]], run_id: str | None, *, all_runs: bool = False) -> list[dict[str, Any]]:
    if all_runs:
        selected = analyses
    elif run_id is not None:
        selected = [row for row in analyses if row.get("analysis_run_id") == run_id]
        if not selected:
            raise ValueError(f"unknown Gemini analysis_run_id: {run_id}")
    else:
        latest = analyses[-1]
        latest_run_id = latest.get("analysis_run_id")
        if latest_run_id:
            selected = [row for row in analyses if row.get("analysis_run_id") == latest_run_id]
        else:
            selected = [latest]
    return sorted(
        selected,
        key=lambda row: (
            str(row.get("source_video_id") or ""),
            _number_or_none(row.get("chunk_start_s")) or 0.0,
            int(row.get("chunk_index") or 0),
        ),
    )


def _chunk_metadata(record: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "analysis_run_id",
        "chunk_id",
        "chunk_index",
        "chunk_start_s",
        "chunk_end_s",
        "chunk_duration_s",
        "time_basis",
        "response_id",
    ]
    return {key: record.get(key) for key in keys if record.get(key) is not None}


def _local_interval(item: dict[str, Any]) -> tuple[float | None, float | None]:
    start_s = _number_or_none(item.get("start_s"))
    end_s = _number_or_none(item.get("end_s"))
    if start_s is not None and end_s is not None and end_s < start_s:
        start_s, end_s = end_s, start_s
    return start_s, end_s


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


def _unique_string_items(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        if value is None:
            continue
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

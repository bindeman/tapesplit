from __future__ import annotations

from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl

TIMED_ANALYSIS_FIELDS = [
    "event_candidates",
    "scene_candidates",
    "person_mentions",
    "place_candidates",
    "date_candidates",
    "language_segments",
    "non_content_ranges",
    "unrelated_ranges",
    "followup_segments",
]

# Chunk timelines that overrun the excerpt by more than this ratio get
# linearly rescaled onto the excerpt instead of clamped/dropped.
TIMELINE_OVERRUN_TOLERANCE = 1.05
# Events narrower than this after normalization are noise (clamp slivers,
# unit confusion), never real footage ranges.
MIN_EVENT_SPAN_S = 5.0
MINUTES_MODE_MIN_EVENTS = 3


def import_gemini_analysis(
    project_dir: Path,
    *,
    run_id: str | None = None,
    all_runs: bool = False,
    best_per_source: bool = True,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    analyses = read_jsonl(project / "gemini_analyses.jsonl")
    if not analyses:
        raise FileNotFoundError(f"missing Gemini analysis file: {project / 'gemini_analyses.jsonl'}")
    if all_runs and run_id is not None:
        raise ValueError("run_id cannot be combined with all_runs")
    selected = _select_analyses(
        analyses,
        run_id,
        all_runs=all_runs,
        best_per_source=best_per_source,
        source_durations=_source_durations(project),
    )
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
    chunks_rescaled = 0
    chunks_minutes_rescaled = 0
    events_resurrected = 0
    events_dropped_sliver = 0
    events_dropped_unreliable = 0

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
        timeline_notes, timeline_stats = _corrected_chunk_timeline(analysis, local_duration_s)
        chunks_rescaled += 1 if timeline_stats["rescaled"] else 0
        chunks_minutes_rescaled += 1 if timeline_stats["minutes_rescaled"] else 0
        events_resurrected += timeline_stats["resurrected_events"]

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
            if item.get("_time_unreliable"):
                events_dropped_unreliable += 1
                skipped.append(
                    {"kind": "event_candidate", "reason": "time_unreliable", "item": item, "chunk": chunk_metadata}
                )
                continue
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
            validation_notes = [*timeline_notes, *validation_notes]
            if "missing_time_range" not in validation_notes and end_s - start_s < MIN_EVENT_SPAN_S:
                events_dropped_sliver += 1
                skipped.append(
                    {"kind": "event_candidate", "reason": "sliver_span", "item": item, "chunk": chunk_metadata}
                )
                continue
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
                validation_notes = [*timeline_notes, *validation_notes]
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
        "selection": "best_per_source" if all_runs and best_per_source else "all" if all_runs else "latest_run",
        "analyses": len(selected),
        "evidence": evidence_count,
        "claims": claim_count,
        "events": event_count,
        "skipped": len(skipped),
        "chunks_rescaled": chunks_rescaled,
        "chunks_minutes_rescaled": chunks_minutes_rescaled,
        "events_resurrected": events_resurrected,
        "events_dropped_sliver": events_dropped_sliver,
        "events_dropped_unreliable": events_dropped_unreliable,
    }


def _corrected_chunk_timeline(
    analysis: dict[str, Any],
    local_duration_s: float | None,
) -> tuple[list[str], dict[str, Any]]:
    """Repair chunk timelines that drifted past the excerpt duration.

    Chunk prompts historically announced the full tape's duration while asking
    for excerpt-relative seconds, so models emitted timelines overrunning the
    excerpt (or, rarely, whole timelines in minutes). Overrun timelines are
    linearly rescaled onto the excerpt — recovering events the old clamp/drop
    path pinned to chunk tails or deleted — and minutes-mode timelines are
    multiplied back into seconds. Mutates the analysis in place; returns notes
    to attach to every imported item plus per-chunk stats.
    """
    stats = {
        "rescaled": False,
        "minutes_rescaled": False,
        "resurrected_events": 0,
    }
    if not local_duration_s or local_duration_s <= 0:
        return [], stats
    max_end = _max_timeline_end(analysis)
    if max_end is None or max_end <= 0:
        return [], stats

    notes: list[str] = []
    events = [item for item in analysis.get("event_candidates") or [] if isinstance(item, dict)]
    event_ends = [end for end in (_number_or_none(event.get("end_s")) for event in events) if end is not None]
    if len(event_ends) >= MINUTES_MODE_MIN_EVENTS and max(event_ends) < local_duration_s / 10:
        if max_end * 60 <= local_duration_s * TIMELINE_OVERRUN_TOLERANCE:
            _scale_timeline(analysis, 60.0)
            max_end *= 60
            notes.append("chunk_timeline_minutes_rescaled")
            stats["minutes_rescaled"] = True
        else:
            # Events sit in the first tenth of the excerpt while other items
            # span it: units are untrustworthy either way.
            for event in events:
                event["_time_unreliable"] = True

    if max_end > local_duration_s * TIMELINE_OVERRUN_TOLERANCE:
        stats["resurrected_events"] = sum(
            1
            for event in events
            if not event.get("_time_unreliable")
            and (_number_or_none(event.get("start_s")) or 0.0) >= local_duration_s
        )
        _scale_timeline(analysis, local_duration_s / max_end)
        notes.append("chunk_timeline_rescaled")
        stats["rescaled"] = True
    return notes, stats


def _max_timeline_end(analysis: dict[str, Any]) -> float | None:
    max_end: float | None = None
    for field in TIMED_ANALYSIS_FIELDS:
        for item in analysis.get(field) or []:
            if not isinstance(item, dict):
                continue
            for key in ("start_s", "end_s"):
                value = _number_or_none(item.get(key))
                if value is not None and (max_end is None or value > max_end):
                    max_end = value
    return max_end


def _scale_timeline(analysis: dict[str, Any], scale: float) -> None:
    for field in TIMED_ANALYSIS_FIELDS:
        for item in analysis.get(field) or []:
            if not isinstance(item, dict):
                continue
            for key in ("start_s", "end_s"):
                value = _number_or_none(item.get(key))
                if value is not None:
                    item[key] = round(value * scale, 3)


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


def _select_analyses(
    analyses: list[dict[str, Any]],
    run_id: str | None,
    *,
    all_runs: bool = False,
    best_per_source: bool = True,
    source_durations: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    if all_runs:
        selected = _best_analyses_per_source(analyses, source_durations or {}) if best_per_source else analyses
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


def _best_analyses_per_source(
    analyses: list[dict[str, Any]],
    source_durations: dict[str, float],
) -> list[dict[str, Any]]:
    indexed = list(enumerate(analyses))
    by_source: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in indexed:
        source_video_id = str(row.get("source_video_id") or "")
        if not source_video_id:
            continue
        by_source.setdefault(source_video_id, []).append((index, row))

    selected: list[dict[str, Any]] = []
    for source_video_id in sorted(by_source):
        source_rows = by_source[source_video_id]
        whole_rows = [
            (index, row)
            for index, row in source_rows
            if str(row.get("time_basis") or "") == "source_video"
            and not _whole_tape_timing_suspect(row, source_durations.get(source_video_id, 0.0))
        ]
        if whole_rows:
            selected.append(max(whole_rows, key=lambda item: item[0])[1])
            continue

        chunk_rows = [
            (index, row)
            for index, row in source_rows
            if str(row.get("time_basis") or "") != "source_video"
        ]
        latest_run_id = _latest_run_id(chunk_rows or source_rows)
        if latest_run_id is None:
            selected.append(max(chunk_rows or source_rows, key=lambda item: item[0])[1])
            continue
        selected.extend(row for _, row in (chunk_rows or source_rows) if row.get("analysis_run_id") == latest_run_id)

    return selected


def _whole_tape_timing_suspect(row: dict[str, Any], duration_s: float) -> bool:
    if str(row.get("time_basis") or "") != "source_video" or duration_s < 1800:
        return False
    analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else {}
    event_count = len(analysis.get("event_candidates") or [])
    if event_count < 3:
        return False
    max_end = 0.0
    for field in [
        "event_candidates",
        "scene_candidates",
        "date_candidates",
        "non_content_ranges",
        "unrelated_ranges",
    ]:
        for item in analysis.get(field) or []:
            if not isinstance(item, dict):
                continue
            end_s = _number_or_none(item.get("end_s"))
            start_s = _number_or_none(item.get("start_s"))
            max_end = max(max_end, end_s or 0.0, start_s or 0.0)
    return bool(max_end and max_end < min(duration_s * 0.12, 600.0))


def _latest_run_id(rows: list[tuple[int, dict[str, Any]]]) -> str | None:
    latest: tuple[int, str] | None = None
    for index, row in rows:
        run_id = row.get("analysis_run_id")
        if not run_id:
            continue
        candidate = (index, str(run_id))
        if latest is None or candidate[0] > latest[0]:
            latest = candidate
    return latest[1] if latest else None


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


def _source_durations(project: Path) -> dict[str, float]:
    return {
        str(row.get("id")): float((row.get("probe") or {}).get("duration_s") or 0.0)
        for row in read_jsonl(project / "tapes.jsonl")
        if row.get("id")
    }


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

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import read_jsonl


def summarize_gemini_analyses(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    analyses = read_jsonl(project / "gemini_analyses.jsonl")
    if source_video_id:
        analyses = [row for row in analyses if row.get("source_video_id") == source_video_id]

    tape_durations = {
        str(row.get("id")): float((row.get("probe") or {}).get("duration_s") or 0.0)
        for row in read_jsonl(project / "tapes.jsonl")
        if row.get("id")
    }
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in analyses:
        source_id = str(row.get("source_video_id") or "unknown")
        run_id = str(row.get("analysis_run_id") or "no_run")
        mode = _analysis_mode(row)
        key = (source_id, run_id, mode)
        bucket = buckets.setdefault(
            key,
            {
                "source_video_id": source_id,
                "filename": row.get("filename"),
                "analysis_run_id": run_id,
                "mode": mode,
                "records": 0,
                "analyzed_duration_s": 0.0,
                "event_candidates": 0,
                "scene_candidates": 0,
                "person_mentions": 0,
                "place_candidates": 0,
                "date_candidates": 0,
                "language_segments": 0,
                "non_content_ranges": 0,
                "unrelated_ranges": 0,
                "followup_segments": 0,
                "uncertainties": 0,
                "relatedness_counts": Counter(),
                "people": set(),
                "places": set(),
                "dates": set(),
                "event_titles": [],
                "followup_reasons": [],
            },
        )
        analysis = row.get("analysis") or {}
        bucket["records"] += 1
        bucket["analyzed_duration_s"] += _analyzed_duration(row, tape_durations.get(source_id, 0.0))
        for field in [
            "event_candidates",
            "scene_candidates",
            "person_mentions",
            "place_candidates",
            "date_candidates",
            "language_segments",
            "non_content_ranges",
            "unrelated_ranges",
            "followup_segments",
            "uncertainties",
        ]:
            bucket[field] += len(analysis.get(field) or [])

        for event in analysis.get("event_candidates") or []:
            if event.get("title"):
                bucket["event_titles"].append(str(event["title"]))
            if event.get("relatedness"):
                bucket["relatedness_counts"][str(event["relatedness"])] += 1
            for person in event.get("people") or []:
                _add_normalized(bucket["people"], person)
            for place in event.get("place_candidates") or []:
                _add_normalized(bucket["places"], place)
            for date in event.get("date_candidates") or []:
                _add_normalized(bucket["dates"], date)

        for person in analysis.get("person_mentions") or []:
            _add_normalized(bucket["people"], person.get("name"))
        for place in analysis.get("place_candidates") or []:
            _add_normalized(bucket["places"], place.get("name"))
        for date in analysis.get("date_candidates") or []:
            _add_normalized(bucket["dates"], date.get("value"))
        for followup in analysis.get("followup_segments") or []:
            if followup.get("reason"):
                bucket["followup_reasons"].append(str(followup["reason"]))

    summaries = [_finalize_summary(bucket) for bucket in buckets.values()]
    summaries.sort(key=lambda row: (row["source_video_id"], row["mode"], row["analysis_run_id"]))
    return {
        "project": str(project),
        "source_video_id": source_video_id,
        "summaries": summaries,
    }


def compare_gemini_analysis_modes(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
) -> dict[str, Any]:
    summary = summarize_gemini_analyses(project_dir, source_video_id=source_video_id)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summary["summaries"]:
        by_source[row["source_video_id"]].append(row)

    comparisons = []
    missing_whole_tape = []
    for source_id, rows in sorted(by_source.items()):
        chunk_rows = [row for row in rows if row["mode"] == "chunk"]
        whole_rows = [row for row in rows if row["mode"] == "whole_tape"]
        if not whole_rows:
            missing_whole_tape.append(source_id)
            continue
        if not chunk_rows:
            comparisons.append({"source_video_id": source_id, "status": "whole_tape_only", "whole_tape": whole_rows[-1]})
            continue
        chunk = _merge_summaries(source_id, "chunk", chunk_rows)
        whole = whole_rows[-1]
        comparisons.append(
            {
                "source_video_id": source_id,
                "status": "compared",
                "chunk": chunk,
                "whole_tape": whole,
                "delta": {
                    "event_candidates": whole["event_candidates"] - chunk["event_candidates"],
                    "unique_people": whole["unique_people"] - chunk["unique_people"],
                    "unique_places": whole["unique_places"] - chunk["unique_places"],
                    "unique_dates": whole["unique_dates"] - chunk["unique_dates"],
                    "unrelated_ranges": whole["unrelated_ranges"] - chunk["unrelated_ranges"],
                    "followup_segments": whole["followup_segments"] - chunk["followup_segments"],
                },
                "whole_only_people": sorted(set(whole["people"]) - set(chunk["people"]))[:25],
                "whole_only_places": sorted(set(whole["places"]) - set(chunk["places"]))[:25],
                "whole_only_dates": sorted(set(whole["dates"]) - set(chunk["dates"]))[:25],
            }
        )

    return {
        "project": summary["project"],
        "source_video_id": source_video_id,
        "comparisons": comparisons,
        "missing_whole_tape": missing_whole_tape,
    }


def _analysis_mode(row: dict[str, Any]) -> str:
    if row.get("time_basis") == "chunk" or row.get("chunk_id"):
        return "chunk"
    return "whole_tape"


def _analyzed_duration(row: dict[str, Any], source_duration_s: float) -> float:
    if row.get("time_basis") == "chunk":
        return float(row.get("chunk_duration_s") or 0.0)
    prepared = row.get("prepared_video") or {}
    return float(prepared.get("duration_s") or source_duration_s or 0.0)


def _add_normalized(values: set[str], raw: Any) -> None:
    if raw is None:
        return
    value = re.sub(r"\s+", " ", str(raw)).strip()
    if value:
        values.add(value)


def _finalize_summary(bucket: dict[str, Any]) -> dict[str, Any]:
    people = sorted(bucket["people"])
    places = sorted(bucket["places"])
    dates = sorted(bucket["dates"])
    return {
        "source_video_id": bucket["source_video_id"],
        "filename": bucket["filename"],
        "analysis_run_id": bucket["analysis_run_id"],
        "mode": bucket["mode"],
        "records": bucket["records"],
        "analyzed_duration_s": round(bucket["analyzed_duration_s"], 3),
        "event_candidates": bucket["event_candidates"],
        "scene_candidates": bucket["scene_candidates"],
        "person_mentions": bucket["person_mentions"],
        "place_candidates": bucket["place_candidates"],
        "date_candidates": bucket["date_candidates"],
        "language_segments": bucket["language_segments"],
        "non_content_ranges": bucket["non_content_ranges"],
        "unrelated_ranges": bucket["unrelated_ranges"],
        "followup_segments": bucket["followup_segments"],
        "uncertainties": bucket["uncertainties"],
        "relatedness_counts": dict(sorted(bucket["relatedness_counts"].items())),
        "unique_people": len(people),
        "unique_places": len(places),
        "unique_dates": len(dates),
        "people": people[:50],
        "places": places[:50],
        "dates": dates[:50],
        "event_titles": bucket["event_titles"][:50],
        "followup_reasons": bucket["followup_reasons"][:25],
    }


def _merge_summaries(source_id: str, mode: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    people = sorted({item for row in rows for item in row["people"]})
    places = sorted({item for row in rows for item in row["places"]})
    dates = sorted({item for row in rows for item in row["dates"]})
    relatedness_counts: Counter[str] = Counter()
    for row in rows:
        relatedness_counts.update(row.get("relatedness_counts") or {})
    return {
        "source_video_id": source_id,
        "filename": rows[-1].get("filename"),
        "analysis_run_id": "merged_chunk_runs",
        "mode": mode,
        "records": sum(row["records"] for row in rows),
        "analyzed_duration_s": round(sum(row["analyzed_duration_s"] for row in rows), 3),
        "event_candidates": sum(row["event_candidates"] for row in rows),
        "scene_candidates": sum(row["scene_candidates"] for row in rows),
        "person_mentions": sum(row["person_mentions"] for row in rows),
        "place_candidates": sum(row["place_candidates"] for row in rows),
        "date_candidates": sum(row["date_candidates"] for row in rows),
        "language_segments": sum(row["language_segments"] for row in rows),
        "non_content_ranges": sum(row["non_content_ranges"] for row in rows),
        "unrelated_ranges": sum(row["unrelated_ranges"] for row in rows),
        "followup_segments": sum(row["followup_segments"] for row in rows),
        "uncertainties": sum(row["uncertainties"] for row in rows),
        "relatedness_counts": dict(sorted(relatedness_counts.items())),
        "unique_people": len(people),
        "unique_places": len(places),
        "unique_dates": len(dates),
        "people": people[:50],
        "places": places[:50],
        "dates": dates[:50],
        "event_titles": [title for row in rows for title in row["event_titles"]][:50],
        "followup_reasons": [reason for row in rows for reason in row["followup_reasons"]][:25],
    }

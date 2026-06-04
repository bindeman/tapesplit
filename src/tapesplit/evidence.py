from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


def build_evidence(project_dir: Path) -> dict:
    project = project_dir.expanduser().resolve()
    output = project / "evidence.jsonl"
    if output.exists():
        output.unlink()

    records = []
    counter = 1

    def add(record: dict[str, Any]) -> None:
        nonlocal counter
        record = {
            "id": f"ev_{counter:06d}",
            "review_status": "unreviewed",
            **record,
        }
        counter += 1
        records.append(record)
        append_jsonl(output, record)

    for row in read_jsonl(project / "media_metadata.jsonl"):
        for candidate in row.get("date_candidates", []):
            add(
                {
                    "source_video_id": row["source_video_id"],
                    "start_s": None,
                    "end_s": None,
                    "modality": "metadata",
                    "kind": "date_candidate",
                    "text": f"{candidate['field']}: {candidate['raw']}",
                    "confidence": candidate.get("confidence", 0.3),
                    "metadata": candidate,
                }
            )

    for row in read_jsonl(project / "non_content_ranges.jsonl"):
        add(
            {
                "source_video_id": row["source_video_id"],
                "start_s": row["start_s"],
                "end_s": row["end_s"],
                "modality": "visual",
                "kind": "non_content_range",
                "text": f"{row['label']} from {_fmt_time(row['start_s'])} to {_fmt_time(row['end_s'])}",
                "confidence": row.get("confidence", 0.75),
                "metadata": row,
            }
        )

    for row in read_jsonl(project / "transcript_segments.jsonl"):
        add(
            {
                "source_video_id": row.get("source_video_id"),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
                "modality": "transcript",
                "kind": "local_transcript_segment",
                "text": row.get("text") or "",
                "language": row.get("language") or "unknown",
                "confidence": row.get("confidence", 0.8),
                "metadata": {
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "transcript_segment_id": row.get("id"),
                },
            }
        )

    seen_transcripts: set[tuple[float, float, str]] = set()
    for search in read_jsonl(project / "twelvelabs_searches.jsonl"):
        query = search.get("query")
        for result in search.get("results", []):
            start = float(result.get("start") or 0.0)
            end = float(result.get("end") or start)
            transcription = _clean_transcript(result.get("transcription"))
            if transcription:
                key = (round(start, 2), round(end, 2), transcription)
                if key in seen_transcripts:
                    continue
                seen_transcripts.add(key)
                add(
                    {
                        "source_video_id": _source_id_for_video(project, result.get("video_id")),
                        "start_s": start,
                        "end_s": end,
                        "modality": "transcript",
                        "kind": "twelvelabs_search_transcript",
                        "text": transcription,
                        "language": "unknown",
                        "confidence": 0.7,
                        "metadata": {
                            "provider": "twelvelabs",
                            "query": query,
                            "rank": result.get("rank"),
                            "video_id": result.get("video_id"),
                        },
                    }
                )
            elif query:
                add(
                    {
                        "source_video_id": _source_id_for_video(project, result.get("video_id")),
                        "start_s": start,
                        "end_s": end,
                        "modality": "search",
                        "kind": "twelvelabs_search_hit",
                        "text": f"Search hit for query: {query}",
                        "confidence": 0.45,
                        "metadata": {
                            "provider": "twelvelabs",
                            "query": query,
                            "rank": result.get("rank"),
                            "video_id": result.get("video_id"),
                        },
                    }
                )

    return {
        "project": str(project),
        "output": str(output),
        "evidence_count": len(records),
        "by_kind": _count_by(records, "kind"),
        "by_modality": _count_by(records, "modality"),
    }


def evidence_for_prompt(project_dir: Path, limit: int = 120) -> list[dict[str, Any]]:
    rows = sorted(
        read_jsonl(project_dir / "evidence.jsonl"),
        key=lambda row: (
            _evidence_prompt_priority(row),
            str(row.get("source_video_id") or ""),
            _number_or_large(row.get("start_s")),
        ),
    )
    useful = []
    for row in rows:
        text = row.get("text") or ""
        if row.get("kind") == "twelvelabs_search_hit" and len(useful) > 40:
            continue
        useful.append(
            {
                "id": row.get("id"),
                "source_video_id": row.get("source_video_id"),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
                "time_label": _range_label(row.get("start_s"), row.get("end_s")),
                "kind": row.get("kind"),
                "modality": row.get("modality"),
                "text": text,
                "confidence": row.get("confidence"),
            }
        )
    return useful[:limit]


def _evidence_prompt_priority(row: dict[str, Any]) -> int:
    kind = row.get("kind")
    modality = row.get("modality")
    if kind in {"local_transcript_segment", "twelvelabs_search_transcript"}:
        return 0
    if modality in {"transcript", "multimodal"}:
        return 1
    if kind == "twelvelabs_search_hit":
        return 2
    if kind == "non_content_range":
        return 3
    if kind == "date_candidate":
        return 4
    return 5


def _source_id_for_video(project: Path, video_id: str | None) -> str | None:
    if not video_id:
        return None
    for row in read_jsonl(project / "twelvelabs_tasks.jsonl"):
        indexed = row.get("indexed_asset") or row.get("task") or {}
        if indexed.get("id") == video_id:
            return row.get("source_video_id")
    return None


def _clean_transcript(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or cleaned in {".", ". .", "..."}:
        return None
    return cleaned


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _number_or_large(value: Any) -> float:
    if value is None or value == "":
        return 1_000_000_000.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1_000_000_000.0


def _range_label(start_s: Any, end_s: Any) -> str | None:
    if start_s is None:
        return None
    if end_s is None:
        return _fmt_time(float(start_s))
    return f"{_fmt_time(float(start_s))}-{_fmt_time(float(end_s))}"


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"

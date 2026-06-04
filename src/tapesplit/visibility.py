from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tapesplit.storage import read_jsonl


EXCLUDED_RELATEDNESS = {"likely_unrelated", "unrelated", "non_content"}
EXCLUDED_ALBUM_TYPES = {"unrelated_content", "non_content"}


@dataclass(frozen=True)
class ExclusionRange:
    source_video_id: str
    start_s: float
    end_s: float
    reason: str
    source_id: str


class VisibilityFilter:
    def __init__(self, ranges: list[ExclusionRange]):
        self.ranges = ranges

    def visible_row(self, row: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]] | None = None) -> bool:
        return not self.excluded_row(row, evidence_by_id=evidence_by_id)

    def excluded_row(self, row: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]] | None = None) -> bool:
        if _row_is_marked_excluded(row):
            return True
        intervals = _row_intervals(row)
        if intervals and all(_interval_excluded(interval, self.ranges) for interval in intervals):
            return True
        evidence_ids = [str(item) for item in row.get("evidence_ids") or [] if item]
        if evidence_ids and evidence_by_id:
            cited = [evidence_by_id[evidence_id] for evidence_id in evidence_ids if evidence_id in evidence_by_id]
            if cited and all(self.excluded_row(item) for item in cited):
                return True
        return False


def build_visibility_filter(project: Path) -> VisibilityFilter:
    return VisibilityFilter(_exclusion_ranges(project))


def _exclusion_ranges(project: Path) -> list[ExclusionRange]:
    events = read_jsonl(project / "canonical_events.jsonl") or (
        read_jsonl(project / "events.jsonl") + read_jsonl(project / "gemini_events.jsonl")
    )
    ranges = []
    for event in events:
        if not _event_is_excluded(event):
            continue
        for interval in _row_intervals(event):
            ranges.append(
                ExclusionRange(
                    source_video_id=interval["source_video_id"],
                    start_s=interval["start_s"],
                    end_s=interval["end_s"],
                    reason=_event_relatedness(event) or str(event.get("album_type") or "excluded"),
                    source_id=str(event.get("id") or ""),
                )
            )
    return _merge_ranges(ranges)


def _row_is_marked_excluded(row: dict[str, Any]) -> bool:
    if _event_is_excluded(row):
        return True
    if str(row.get("export_status") or "").lower() == "excluded":
        return True
    if str(row.get("review_status") or "").lower() == "excluded":
        return True
    if str(row.get("album_type") or "").lower() in EXCLUDED_ALBUM_TYPES:
        return True
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if str(metadata.get("review_status") or "").lower() == "excluded":
        return True
    return False


def _event_is_excluded(row: dict[str, Any]) -> bool:
    return _event_relatedness(row) in EXCLUDED_RELATEDNESS


def _event_relatedness(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return str(row.get("relatedness") or metadata.get("relatedness") or "").lower()


def _row_intervals(row: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    intervals = []
    for source in [row.get("source_ranges"), metadata.get("source_ranges")]:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            interval = _interval_from_values(item.get("source_video_id"), item.get("start_s"), item.get("end_s"))
            if interval:
                intervals.append(interval)

    source_video_id = row.get("source_video_id")
    if not source_video_id:
        source_video_ids = row.get("source_video_ids") or metadata.get("source_video_ids")
        if isinstance(source_video_ids, list) and len(source_video_ids) == 1:
            source_video_id = source_video_ids[0]
    interval = _interval_from_values(source_video_id, row.get("start_s"), row.get("end_s"))
    if interval:
        intervals.append(interval)

    seen = set()
    unique = []
    for interval in intervals:
        key = (interval["source_video_id"], interval["start_s"], interval["end_s"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(interval)
    return unique


def _interval_from_values(source_video_id: Any, start_s: Any, end_s: Any) -> dict[str, Any] | None:
    source = str(source_video_id or "")
    start = _number_or_none(start_s)
    end = _number_or_none(end_s)
    if not source or start is None:
        return None
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    return {"source_video_id": source, "start_s": start, "end_s": end}


def _interval_excluded(interval: dict[str, Any], ranges: list[ExclusionRange]) -> bool:
    for exclusion in ranges:
        if exclusion.source_video_id != interval["source_video_id"]:
            continue
        if _overlaps_enough(interval["start_s"], interval["end_s"], exclusion.start_s, exclusion.end_s):
            return True
    return False


def _overlaps_enough(start: float, end: float, excluded_start: float, excluded_end: float) -> bool:
    if end <= start:
        return excluded_start <= start <= excluded_end
    overlap = max(0.0, min(end, excluded_end) - max(start, excluded_start))
    if overlap <= 0:
        return False
    duration = max(0.001, end - start)
    return overlap / duration >= 0.5 or excluded_start <= start <= excluded_end


def _merge_ranges(ranges: list[ExclusionRange]) -> list[ExclusionRange]:
    merged = []
    for item in sorted(ranges, key=lambda row: (row.source_video_id, row.start_s, row.end_s)):
        if not merged or merged[-1].source_video_id != item.source_video_id or item.start_s > merged[-1].end_s:
            merged.append(item)
            continue
        previous = merged[-1]
        merged[-1] = ExclusionRange(
            source_video_id=previous.source_video_id,
            start_s=previous.start_s,
            end_s=max(previous.end_s, item.end_s),
            reason=previous.reason,
            source_id=previous.source_id,
        )
    return merged


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from tapesplit.storage import read_jsonl
from tapesplit.visibility import build_visibility_filter


def export_story(
    project_dir: Path,
    *,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in evidence_rows if row.get("id")}
    visibility = build_visibility_filter(project)

    events = [
        row
        for row in read_jsonl(project / "canonical_events.jsonl")
        if visibility.visible_row(row, evidence_by_id=evidence_by_id)
    ]
    events_by_id = {str(row.get("id") or ""): row for row in events if row.get("id")}
    reconciliations_by_event = {
        str(row.get("canonical_event_id") or ""): row
        for row in read_jsonl(project / "event_reconciliations.jsonl")
        if row.get("canonical_event_id")
    }
    albums = [
        row
        for row in read_jsonl(project / "albums.jsonl")
        if visibility.visible_row(row, evidence_by_id=evidence_by_id) and str(row.get("export_status") or "") != "excluded"
    ]
    chapters = []
    covered_event_ids: set[str] = set()
    for album in _dedupe_albums(albums):
        chapter = _story_chapter(album, events_by_id=events_by_id, reconciliations_by_event=reconciliations_by_event)
        chapter["events"] = [event for event in chapter["events"] if event["id"] not in covered_event_ids]
        if not chapter["events"]:
            continue
        chapter["summary"] = _chapter_summary(album, chapter["events"])
        covered_event_ids.update(str(event["id"]) for event in chapter["events"])
        chapters.append(chapter)
    story = {
        "project": str(project),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "chapter_count": len(chapters),
        "event_count": sum(len(chapter["events"]) for chapter in chapters),
        "chapters": chapters,
    }

    json_path = (out_json.expanduser().resolve() if out_json else project / "story.json")
    md_path = (out_md.expanduser().resolve() if out_md else project / "tape_story.md")
    json_path.write_text(json.dumps(story, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_story_markdown(story), encoding="utf-8")
    return {
        "project": str(project),
        "story_json": str(json_path),
        "story_markdown": str(md_path),
        "chapters": story["chapter_count"],
        "events": story["event_count"],
    }


def _story_chapter(
    album: dict[str, Any],
    *,
    events_by_id: dict[str, dict[str, Any]],
    reconciliations_by_event: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    event_ids = [str(event_id) for event_id in album.get("canonical_event_ids") or [] if event_id]
    events = [
        _story_event(events_by_id[event_id], reconciliations_by_event.get(event_id))
        for event_id in event_ids
        if event_id in events_by_id
    ]
    return {
        "id": str(album.get("id") or ""),
        "title": str(album.get("title") or "Untitled album"),
        "album_type": album.get("album_type"),
        "date_label": album.get("date_label"),
        "place_label": album.get("place_label"),
        "people_labels": album.get("people_labels") or [],
        "language_labels": album.get("language_labels") or [],
        "source_video_ids": album.get("source_video_ids") or [],
        "start_s": album.get("start_s"),
        "end_s": album.get("end_s"),
        "confidence": album.get("confidence"),
        "review_status": album.get("review_status"),
        "summary": _chapter_summary(album, events),
        "events": events,
    }


def _story_event(event: dict[str, Any], reconciliation: dict[str, Any] | None) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_ranges = _event_source_ranges(event)
    primary_range = source_ranges[0] if source_ranges else {}
    return {
        "id": str(event.get("id") or ""),
        "title": str((reconciliation or {}).get("reconciled_title") or event.get("title") or "Untitled event"),
        "original_title": event.get("title"),
        "summary": str((reconciliation or {}).get("reconciled_summary") or event.get("summary") or ""),
        "event_type": metadata.get("event_type"),
        "relatedness": event.get("relatedness") or metadata.get("relatedness"),
        "source_video_ids": _unique_items(
            [str(row.get("source_video_id") or "") for row in source_ranges if row.get("source_video_id")]
            or [str(item) for item in event.get("source_video_ids") or metadata.get("source_video_ids") or [] if item]
        ),
        "source_ranges": source_ranges,
        "start_s": primary_range.get("start_s", event.get("start_s")),
        "end_s": primary_range.get("end_s", event.get("end_s")),
        "timeline_start_s": event.get("start_s"),
        "timeline_end_s": event.get("end_s"),
        "confidence": event.get("confidence"),
        "reconciliation_status": (reconciliation or {}).get("reconciliation_status"),
        "selected_place_labels": (reconciliation or {}).get("selected_place_labels") or [],
        "rejected_place_labels": (reconciliation or {}).get("rejected_place_labels") or [],
    }


def _chapter_summary(album: dict[str, Any], events: list[dict[str, Any]]) -> str:
    parts = []
    date_label = str(album.get("date_label") or "").strip()
    place_label = str(album.get("place_label") or "").strip()
    if date_label or place_label:
        parts.append(" / ".join(value for value in [date_label, place_label] if value))
    titles = [str(event.get("title") or "") for event in events[:4] if event.get("title")]
    if titles:
        parts.append("; ".join(titles))
    return ". ".join(parts)


def _story_markdown(story: dict[str, Any]) -> str:
    lines = ["# Tape Story", ""]
    for chapter in story["chapters"]:
        lines.extend([f"## {chapter['title']}", ""])
        facts = [
            ("Date", chapter.get("date_label")),
            ("Place", chapter.get("place_label")),
            ("People", ", ".join(chapter.get("people_labels") or [])),
            ("Source", ", ".join(chapter.get("source_video_ids") or [])),
        ]
        for label, value in facts:
            if value:
                lines.append(f"- **{label}:** {value}")
        if chapter.get("summary"):
            lines.extend(["", str(chapter["summary"])])
        lines.append("")
        for event in chapter["events"]:
            time_label = _range_label(event.get("start_s"), event.get("end_s"))
            suffix = f" ({time_label})" if time_label else ""
            lines.append(f"- **{event['title']}**{suffix}: {event.get('summary') or ''}".rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _dedupe_albums(albums: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for album in albums:
        event_ids = sorted(str(event_id) for event_id in album.get("canonical_event_ids") or [] if event_id)
        key = f"{album.get('album_type') or 'album'}:{'|'.join(event_ids)}" if event_ids else str(album.get("id") or "")
        current = buckets.get(key)
        if current is None or _album_rank(album) > _album_rank(current):
            buckets[key] = album
    return sorted(buckets.values(), key=lambda album: _number_or_large(album.get("start_s")))


def _event_source_ranges(event: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    ranges = metadata.get("source_ranges") if isinstance(metadata.get("source_ranges"), list) else []
    clean_ranges = []
    for row in ranges:
        if not isinstance(row, dict) or not row.get("source_video_id"):
            continue
        clean_ranges.append(
            {
                "source_video_id": str(row.get("source_video_id") or ""),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
            }
        )
    return clean_ranges


def _album_rank(album: dict[str, Any]) -> tuple[int, int, float]:
    return (
        1 if album.get("cover_event_id") else 0,
        1 if str(album.get("review_status") or "") == "unreviewed" else 0,
        _number_or_none(album.get("confidence")) or 0.0,
    )


def _range_label(start: Any, end: Any) -> str:
    start_number = _number_or_none(start)
    end_number = _number_or_none(end)
    if start_number is None:
        return ""
    if end_number is None:
        return _format_time(start_number)
    return f"{_format_time(start_number)}-{_format_time(end_number)}"


def _format_time(value: float) -> str:
    minutes = int(value // 60)
    seconds = int(value % 60)
    return f"{minutes}:{seconds:02d}"


def _unique_items(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = str(value).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(str(value))
    return result


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_or_large(value: Any) -> float:
    number = _number_or_none(value)
    return number if number is not None else 1_000_000_000.0

"""Local heuristic event synthesis for tapes without cloud/LLM analysis.

TapeSplit's richest events come from Gemini video analysis (``gemini_events.jsonl``)
or the legacy Azure claim extractor (``events.jsonl``). Both are cloud calls. When
neither has covered a tape, this module synthesizes low-confidence "recording
segment" events from purely local signals:

- scene intervals (``scenes.jsonl``), which already fold in non-content ranges
- non-content ranges (``non_content_ranges.jsonl``) as a fallback splitter
- transcript segments (``transcript_segments.jsonl``) for keywords/languages

Synthesized events use the same shape as other source events so
``event_stitching`` can merge them into ``canonical_events.jsonl``. They are
marked ``source: "local_heuristic"`` with low confidence so review tooling and
auto-acceptance treat them as weak, replaceable guesses.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl

DEFAULT_MIN_EVENT_SECONDS = 10.0
DEFAULT_MERGE_GAP_SECONDS = 15.0
DEFAULT_MAX_TITLE_KEYWORDS = 4
HEURISTIC_EVENT_SOURCE = "local_heuristic"
HEURISTIC_EVENT_CONFIDENCE = 0.25

_KEYWORD_PATTERN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

_KEYWORD_STOPWORDS = {
    # English function/filler words common in home-video narration.
    "and", "are", "but", "can", "come", "did", "don", "for", "get", "going",
    "got", "had", "has", "have", "her", "here", "him", "his", "how", "into",
    "just", "know", "like", "look", "not", "now", "okay", "one", "our", "out",
    "put", "say", "see", "she", "that", "the", "them", "then", "there",
    "they", "this", "very", "want", "was", "were", "what", "when", "where",
    "who", "will", "with", "yeah", "yes", "you", "your",
}


def build_heuristic_events(
    project_dir: Path,
    *,
    min_event_seconds: float = DEFAULT_MIN_EVENT_SECONDS,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
    only_uncovered_sources: bool = True,
) -> dict[str, Any]:
    """Write ``heuristic_events.jsonl`` for tapes with no analyzed events."""

    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")

    scenes_by_source = _group_by_source(read_jsonl(project / "scenes.jsonl"))
    non_content_by_source = _group_by_source(read_jsonl(project / "non_content_ranges.jsonl"))
    transcripts_by_source = _group_by_source(read_jsonl(project / "transcript_segments.jsonl"))
    covered = analyzed_source_video_ids(project) if only_uncovered_sources else set()

    events: list[dict[str, Any]] = []
    skipped_covered = []
    for tape in tapes:
        source_video_id = str(tape.get("id") or "")
        if not source_video_id:
            continue
        if source_video_id in covered:
            skipped_covered.append(source_video_id)
            continue
        duration_s = _number_or_none((tape.get("probe") or {}).get("duration_s")) or 0.0
        spans = _content_spans(
            duration_s=duration_s,
            scenes=scenes_by_source.get(source_video_id, []),
            non_content_ranges=non_content_by_source.get(source_video_id, []),
        )
        spans = _merge_spans(spans, merge_gap_seconds=merge_gap_seconds)
        kept = [span for span in spans if span[1] - span[0] >= min_event_seconds]
        if not kept and spans:
            kept = [max(spans, key=lambda span: span[1] - span[0])]
        transcripts = transcripts_by_source.get(source_video_id, [])
        for index, (start_s, end_s) in enumerate(kept, start=1):
            events.append(
                _make_event(
                    tape=tape,
                    source_video_id=source_video_id,
                    index=index,
                    start_s=start_s,
                    end_s=end_s,
                    transcripts=_segments_in_range(transcripts, start_s, end_s),
                )
            )

    output = project / "heuristic_events.jsonl"
    if output.exists():
        output.unlink()
    for event in events:
        append_jsonl(output, event)

    return {
        "project": str(project),
        "output": str(output),
        "heuristic_events": len(events),
        "sources_with_events": len({event["source_video_id"] for event in events}),
        "sources_skipped_covered": skipped_covered,
    }


def analyzed_source_video_ids(project: Path) -> set[str]:
    """Source videos already covered by Gemini or legacy analyzed events."""

    covered = set()
    for filename in ("gemini_events.jsonl", "events.jsonl"):
        for row in read_jsonl(project / filename):
            covered.update(_event_source_video_ids(row))
    return covered


def _event_source_video_ids(event: dict[str, Any]) -> set[str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    values = set()
    if event.get("source_video_id"):
        values.add(str(event["source_video_id"]))
    for source in (event.get("source_video_ids"), metadata.get("source_video_ids")):
        if isinstance(source, list):
            values.update(str(item) for item in source if item)
    for item in metadata.get("source_ranges") or []:
        if isinstance(item, dict) and item.get("source_video_id"):
            values.add(str(item["source_video_id"]))
    return values


def _content_spans(
    *,
    duration_s: float,
    scenes: list[dict[str, Any]],
    non_content_ranges: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    content_scenes = [
        (
            _number_or_none(scene.get("start_s")) or 0.0,
            _number_or_none(scene.get("end_s")) or 0.0,
        )
        for scene in scenes
        if scene.get("scene_type") == "content"
    ]
    content_scenes = [(start, end) for start, end in content_scenes if end > start]
    if content_scenes:
        return sorted(content_scenes)

    if duration_s <= 0:
        return []

    blocked = sorted(
        (
            max(0.0, _number_or_none(item.get("start_s")) or 0.0),
            min(duration_s, _number_or_none(item.get("end_s")) or 0.0),
        )
        for item in non_content_ranges
    )
    spans = []
    cursor = 0.0
    for start, end in blocked:
        if end <= start:
            continue
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_s:
        spans.append((cursor, duration_s))
    return spans


def _merge_spans(
    spans: list[tuple[float, float]],
    *,
    merge_gap_seconds: float,
) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start - merged[-1][1] <= merge_gap_seconds:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _segments_in_range(
    transcripts: list[dict[str, Any]],
    start_s: float,
    end_s: float,
) -> list[dict[str, Any]]:
    selected = []
    for segment in transcripts:
        seg_start = _number_or_none(segment.get("start_s"))
        seg_end = _number_or_none(segment.get("end_s"))
        if seg_start is None:
            continue
        if seg_end is None:
            seg_end = seg_start
        if seg_end >= start_s and seg_start <= end_s:
            selected.append(segment)
    return selected


def _make_event(
    *,
    tape: dict[str, Any],
    source_video_id: str,
    index: int,
    start_s: float,
    end_s: float,
    transcripts: list[dict[str, Any]],
) -> dict[str, Any]:
    keywords = _keywords(transcripts, limit=DEFAULT_MAX_TITLE_KEYWORDS)
    languages = _languages(transcripts)
    filename = str(tape.get("filename") or source_video_id)

    if keywords:
        title = " ".join(word.capitalize() for word in keywords)
        mention_note = f" Appears to mention: {', '.join(keywords)}."
    else:
        title = f"Recording segment {index}"
        mention_note = " No transcript available for this segment."
    summary = (
        f"Auto-detected recording segment {index} of {filename} "
        f"({_format_clock(start_s)}–{_format_clock(end_s)})."
        f"{mention_note}"
    )

    return {
        "id": f"heuristic_event_{source_video_id}_{index:04d}",
        "source": HEURISTIC_EVENT_SOURCE,
        "source_video_id": source_video_id,
        "review_status": "unreviewed",
        "title": title,
        "summary": summary,
        "start_s": round(start_s, 3),
        "end_s": round(end_s, 3),
        "confidence": HEURISTIC_EVENT_CONFIDENCE,
        "evidence_ids": [],
        "metadata": {
            "event_type": None,
            "people": [],
            "place_candidates": [],
            "date_candidates": [],
            "languages": languages,
            "source_video_ids": [source_video_id],
            "source_ranges": [
                {
                    "source_video_id": source_video_id,
                    "start_s": round(start_s, 3),
                    "end_s": round(end_s, 3),
                }
            ],
            "heuristic": True,
            "generator": "heuristic_events_v1",
            "transcript_segment_count": len(transcripts),
            "keywords": keywords,
        },
    }


def _keywords(transcripts: list[dict[str, Any]], *, limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    proper_nouns: Counter[str] = Counter()
    for segment in transcripts:
        text = str(segment.get("text") or "")
        for match in _KEYWORD_PATTERN.finditer(text):
            token = match.group(0)
            lowered = token.lower()
            if lowered in _KEYWORD_STOPWORDS:
                continue
            counts[lowered] += 1
            # Capitalized mid-sentence tokens are likely names/places.
            if token[:1].isupper() and match.start() > 0 and text[match.start() - 1] not in ".!?\n":
                proper_nouns[lowered] += 1

    scored = Counter()
    for token, count in counts.items():
        scored[token] = count + 2 * proper_nouns.get(token, 0)
    return [token for token, _ in scored.most_common(limit)]


def _languages(transcripts: list[dict[str, Any]]) -> list[str]:
    values = []
    for segment in transcripts:
        language = str(segment.get("language") or "").strip()
        if language and language.lower() != "auto" and language not in values:
            values.append(language)
    return values


def _group_by_source(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source_video_id = str(row.get("source_video_id") or "")
        if source_video_id:
            grouped.setdefault(source_video_id, []).append(row)
    return grouped


def _format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

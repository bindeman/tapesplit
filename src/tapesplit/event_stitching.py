from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


GENERIC_PLACE_TOKENS = {
    "beach",
    "classroom",
    "crater",
    "garden",
    "hawaii",
    "home",
    "house",
    "kitchen",
    "living",
    "ocean",
    "oregon",
    "park",
    "school",
    "street",
    "volcano",
}

GENERIC_PERSON_TOKENS = {
    "filip",
    "philip",
    "boy",
    "child",
    "adult",
    "family",
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "day",
    "event",
    "family",
    "first",
    "in",
    "into",
    "of",
    "on",
    "the",
    "to",
    "trip",
    "visit",
    "with",
}


def stitch_project_events(
    project_dir: Path,
    *,
    max_gap_seconds: float = 120.0,
    prefer_gemini: bool = True,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    source_events = load_source_events(project, prefer_gemini=prefer_gemini)
    stitched = stitch_events(source_events, max_gap_seconds=max_gap_seconds)

    output = project / "canonical_events.jsonl"
    if output.exists():
        output.unlink()
    for event in stitched:
        append_jsonl(output, event)

    merged_groups = sum(1 for event in stitched if event.get("metadata", {}).get("source_event_count", 0) > 1)
    return {
        "project": str(project),
        "source_events": len(source_events),
        "canonical_events": len(stitched),
        "merged_groups": merged_groups,
        "output": str(output),
    }


def load_source_events(project: Path, *, prefer_gemini: bool = True) -> list[dict[str, Any]]:
    gemini_events = read_jsonl(project / "gemini_events.jsonl")
    if prefer_gemini and gemini_events:
        return [_normalize_candidate(row) for row in gemini_events]
    return [_normalize_candidate(row) for row in read_jsonl(project / "events.jsonl") + gemini_events]


def stitch_events(
    events: list[dict[str, Any]],
    *,
    max_gap_seconds: float = 120.0,
) -> list[dict[str, Any]]:
    normalized = [_normalize_candidate(row) for row in events]
    candidates = sorted(
        [row for row in normalized if _number_or_none(row.get("start_s")) is not None],
        key=lambda row: (_number_or_none(row.get("start_s")) or 0.0, _number_or_none(row.get("end_s")) or 0.0),
    )
    clusters: list[dict[str, Any]] = []
    for event in candidates:
        if not clusters:
            clusters.append(_new_cluster(event))
            continue
        current = clusters[-1]
        should_merge, reason = _should_merge(current, event, max_gap_seconds=max_gap_seconds)
        if should_merge:
            _add_to_cluster(current, event, reason)
        else:
            clusters.append(_new_cluster(event))

    return [_cluster_to_event(index, cluster) for index, cluster in enumerate(clusters, start=1)]


def _normalize_candidate(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    start_s = _number_or_none(event.get("start_s"))
    end_s = _number_or_none(event.get("end_s"))
    if start_s is not None and end_s is not None and end_s < start_s:
        start_s, end_s = end_s, start_s
    if start_s is not None and end_s is None:
        end_s = start_s
    if end_s is not None and start_s is None:
        start_s = end_s
    return {
        **event,
        "start_s": start_s,
        "end_s": end_s,
        "_event_type": metadata.get("event_type") or event.get("event_type"),
        "_people": _string_list(metadata.get("people") or event.get("people")),
        "_places": _string_list(metadata.get("place_candidates") or event.get("place_candidates")),
        "_dates": _string_list(metadata.get("date_candidates") or event.get("date_candidates")),
        "_languages": _string_list(metadata.get("languages") or event.get("languages")),
        "_title_tokens": _content_tokens(event.get("title")),
        "_place_tokens": _entity_tokens(metadata.get("place_candidates") or event.get("place_candidates"), GENERIC_PLACE_TOKENS),
        "_person_tokens": _entity_tokens(metadata.get("people") or event.get("people"), GENERIC_PERSON_TOKENS),
    }


def _new_cluster(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [event],
        "start_s": event.get("start_s"),
        "end_s": event.get("end_s"),
        "merge_reasons": [],
    }


def _add_to_cluster(cluster: dict[str, Any], event: dict[str, Any], reason: str) -> None:
    cluster["events"].append(event)
    cluster["start_s"] = min(_number_or_none(cluster.get("start_s")) or 0.0, _number_or_none(event.get("start_s")) or 0.0)
    cluster["end_s"] = max(_number_or_none(cluster.get("end_s")) or 0.0, _number_or_none(event.get("end_s")) or 0.0)
    cluster["merge_reasons"].append(reason)


def _should_merge(
    cluster: dict[str, Any],
    event: dict[str, Any],
    *,
    max_gap_seconds: float,
) -> tuple[bool, str]:
    prior_events = cluster["events"]
    last = prior_events[-1]
    cluster_end = _number_or_none(cluster.get("end_s")) or 0.0
    event_start = _number_or_none(event.get("start_s")) or 0.0
    gap = event_start - cluster_end
    if gap > max_gap_seconds:
        return False, "gap_too_large"

    best_score = 0.0
    best_reason = ""
    for prior in prior_events:
        score, reason = _merge_score(prior, event, gap_seconds=gap)
        if score > best_score:
            best_score = score
            best_reason = reason
    return best_score >= 4.0, best_reason


def _merge_score(left: dict[str, Any], right: dict[str, Any], *, gap_seconds: float) -> tuple[float, str]:
    score = 0.0
    reasons = []

    title_similarity = _jaccard(left.get("_title_tokens", set()), right.get("_title_tokens", set()))
    if title_similarity >= 0.55:
        score += 3.0
        reasons.append("similar_title")
    elif title_similarity >= 0.35:
        score += 1.5
        reasons.append("partly_similar_title")

    left_type = left.get("_event_type")
    right_type = right.get("_event_type")
    if left_type and right_type and left_type == right_type:
        score += 1.0
        reasons.append("same_type")
    elif left_type and right_type and left_type != right_type:
        score -= 2.0
        reasons.append("different_type")

    shared_places = set(left.get("_place_tokens", set())) & set(right.get("_place_tokens", set()))
    if shared_places:
        score += 2.0
        reasons.append("shared_place")

    shared_dates = _normalized_values(left.get("_dates", [])) & _normalized_values(right.get("_dates", []))
    if shared_dates:
        score += 2.0
        reasons.append("shared_date")
    elif left.get("_dates") and right.get("_dates"):
        score -= 1.0
        reasons.append("different_dates")

    shared_people = set(left.get("_person_tokens", set())) & set(right.get("_person_tokens", set()))
    if shared_people and title_similarity >= 0.25:
        score += 1.0
        reasons.append("shared_person")

    if gap_seconds <= 0:
        score += 0.75
        reasons.append("overlap")
    elif gap_seconds <= 30:
        score += 0.5
        reasons.append("nearby")

    if _has_chunk_boundary_note(left) or _has_chunk_boundary_note(right):
        score += 0.75
        reasons.append("chunk_boundary")

    return score, "+".join(reasons) or "weak_match"


def _cluster_to_event(index: int, cluster: dict[str, Any]) -> dict[str, Any]:
    events = cluster["events"]
    best = max(events, key=lambda row: (float(row.get("confidence") or 0.0), len(str(row.get("title") or ""))))
    source_event_ids = [str(row.get("id")) for row in events if row.get("id")]
    evidence_ids = _unique_string_items(item for row in events for item in row.get("evidence_ids", []))
    summaries = _unique_string_items(row.get("summary") for row in events if row.get("summary"))
    people = _unique_string_items(item for row in events for item in row.get("_people", []))
    places = _unique_string_items(item for row in events for item in row.get("_places", []))
    dates = _unique_string_items(item for row in events for item in row.get("_dates", []))
    languages = _unique_string_items(item for row in events for item in row.get("_languages", []))
    event_type = _most_common(row.get("_event_type") for row in events if row.get("_event_type"))
    relatedness = _most_common(row.get("relatedness") for row in events if row.get("relatedness"))
    review_status = "needs_review" if len(events) > 1 or any(row.get("review_status") == "needs_review" for row in events) else "unreviewed"
    confidence_values = [float(row.get("confidence")) for row in events if isinstance(row.get("confidence"), (int, float))]
    confidence = round(sum(confidence_values) / len(confidence_values), 3) if confidence_values else None

    metadata = {
        "event_type": event_type,
        "people": people,
        "place_candidates": places,
        "date_candidates": dates,
        "languages": languages,
        "source_event_ids": source_event_ids,
        "source_event_count": len(events),
        "merge_reasons": cluster.get("merge_reasons", []),
    }
    return {
        "id": f"canonical_event_{index:06d}",
        "source": "event_stitcher",
        "review_status": review_status,
        "title": str(best.get("title") or "Untitled event"),
        "start_s": round(float(cluster.get("start_s") or 0.0), 3),
        "end_s": round(float(cluster.get("end_s") or 0.0), 3),
        "confidence": confidence,
        "evidence_ids": evidence_ids,
        "summary": _merge_summaries(summaries),
        "relatedness": relatedness,
        "metadata": metadata,
    }


def _merge_summaries(summaries: list[str]) -> str:
    if not summaries:
        return ""
    if len(summaries) == 1:
        return summaries[0]
    text = " ".join(summary.rstrip(".") + "." for summary in summaries[:3])
    return text[:900].rstrip()


def _has_chunk_boundary_note(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    notes = metadata.get("validation_notes") if isinstance(metadata.get("validation_notes"), list) else []
    return any("chunk" in str(note) for note in notes)


def _content_tokens(value: Any) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    return {token for token in tokens if len(token) > 2 and token not in STOPWORDS}


def _entity_tokens(values: Any, generic_tokens: set[str]) -> set[str]:
    tokens = set()
    for value in _string_list(values):
        item_tokens = _content_tokens(value)
        if not item_tokens:
            continue
        distinctive = item_tokens - generic_tokens
        tokens.update(distinctive)
    return tokens


def _normalized_values(values: Any) -> set[str]:
    normalized = set()
    for value in _string_list(values):
        tokens = _content_tokens(value)
        if tokens:
            normalized.add(" ".join(sorted(tokens)))
    return normalized


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _string_list(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(item) for item in values if item not in (None, "")]
    if values in (None, ""):
        return []
    return [str(values)]


def _unique_string_items(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _most_common(values: Any) -> str | None:
    cleaned = [str(value) for value in values if value not in (None, "")]
    if not cleaned:
        return None
    return Counter(cleaned).most_common(1)[0][0]


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from tapesplit.event_stitching import load_source_events
from tapesplit.storage import append_jsonl, read_jsonl


ROLE_PERSON_TOKENS = {
    "adult",
    "adults",
    "boy",
    "boys",
    "child",
    "children",
    "family",
    "families",
    "friend",
    "friends",
    "girl",
    "girls",
    "grandma",
    "grandmas",
    "grandmother",
    "grandmothers",
    "kid",
    "kids",
    "man",
    "men",
    "person",
    "people",
    "teacher",
    "teachers",
    "woman",
    "women",
}

PERSON_ALIAS_KEYS = {
    "filip": "filip",
    "philip": "filip",
    "filipp": "filip",
    "philipp": "filip",
}

ROOM_PLACE_TOKENS = {
    "bedroom",
    "child room",
    "child's room",
    "hallway",
    "home",
    "house",
    "kitchen",
    "living room",
    "room",
}

GENERIC_PLACE_TOKENS = ROOM_PLACE_TOKENS | {
    "beach",
    "botanical garden",
    "crater",
    "garden",
    "hot springs",
    "library",
    "ocean",
    "park",
    "rainforest",
    "restaurant",
    "school",
    "street",
    "sulfur springs",
    "volcano",
    "waterfalls",
}

MENTIONED_PLACE_CONTEXT_TOKENS = {
    "czechia",
    "hapsburg",
    "otyap",
    "uzbekistan",
}

BROAD_PLACE_ALIASES = {
    "hawaii": {
        "hawaii",
        "hilo",
        "hualalai",
        "kilauea",
        "mauna kea",
        "pahoehoe",
    },
    "oregon": {
        "cascade mountains",
        "eugene",
        "eugene oregon",
        "interstate 5",
        "oregon",
        "spencer butte",
        "willamette river",
    },
    "moscow": {
        "moscow",
        "school no 123",
        "school no. 123",
    },
}

DATE_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def build_project_groups(project_dir: Path, *, prefer_canonical: bool = True) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    events = _load_groupable_events(project, prefer_canonical=prefer_canonical)
    evidence = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in evidence if row.get("id")}

    people_groups = _build_people_groups(events, evidence_by_id)
    place_groups = _build_place_groups(events, evidence_by_id)
    date_groups = _build_date_groups(events, evidence_by_id)
    language_groups = _build_language_groups(events, evidence_by_id)
    event_groups = _build_event_groups(events, date_groups)
    albums = _build_albums(events, event_groups, evidence_by_id)

    outputs = {
        "people_groups": project / "people_groups.jsonl",
        "place_groups": project / "place_groups.jsonl",
        "date_groups": project / "date_groups.jsonl",
        "language_groups": project / "language_groups.jsonl",
        "event_groups": project / "event_groups.jsonl",
        "albums": project / "albums.jsonl",
    }
    _write_jsonl(outputs["people_groups"], people_groups)
    _write_jsonl(outputs["place_groups"], place_groups)
    _write_jsonl(outputs["date_groups"], date_groups)
    _write_jsonl(outputs["language_groups"], language_groups)
    _write_jsonl(outputs["event_groups"], event_groups)
    _write_jsonl(outputs["albums"], albums)

    return {
        "project": str(project),
        "source_events": len(events),
        "people_groups": len(people_groups),
        "place_groups": len(place_groups),
        "date_groups": len(date_groups),
        "language_groups": len(language_groups),
        "event_groups": len(event_groups),
        "albums": len(albums),
        "outputs": {name: str(path) for name, path in outputs.items()},
    }


def _load_groupable_events(project: Path, *, prefer_canonical: bool) -> list[dict[str, Any]]:
    canonical = read_jsonl(project / "canonical_events.jsonl")
    if prefer_canonical and canonical:
        return sorted(canonical, key=lambda row: _number_or_large(row.get("start_s")))
    return sorted(load_source_events(project), key=lambda row: _number_or_large(row.get("start_s")))


def _build_people_groups(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        for person in _metadata_list(event, "people"):
            key, kind = _person_key_and_kind(person)
            bucket = buckets.setdefault(
                key,
                {
                    "kind": kind,
                    "aliases": [],
                    "events": [],
                    "mentions": [],
                },
            )
            bucket["kind"] = "role_candidate" if kind == "role_candidate" else bucket["kind"]
            if person not in bucket["aliases"]:
                bucket["aliases"].append(person)
            bucket["events"].append(event)
            bucket["mentions"].append(
                {
                    "alias": person,
                    "canonical_event_id": event.get("id"),
                    "event_title": event.get("title"),
                    "start_s": event.get("start_s"),
                    "end_s": event.get("end_s"),
                }
            )

    groups = []
    for index, (key, bucket) in enumerate(sorted(buckets.items()), start=1):
        aliases = _sorted_labels(bucket["aliases"])
        events_for_group = _unique_events(bucket["events"])
        event_ids = _event_ids(events_for_group)
        evidence_ids = _event_evidence_ids(events_for_group)
        source_video_ids = _source_video_ids(evidence_ids, evidence_by_id)
        kind = bucket["kind"]
        review_status = "needs_review" if kind == "role_candidate" or len(aliases) > 1 else "unreviewed"
        groups.append(
            {
                "id": f"people_group_{index:06d}",
                "kind": kind,
                "label": " / ".join(aliases),
                "aliases": aliases,
                "canonical_event_ids": event_ids,
                "evidence_ids": evidence_ids,
                "source_video_ids": source_video_ids,
                "first_start_s": _first_start(events_for_group),
                "last_end_s": _last_end(events_for_group),
                "mention_count": len(bucket["mentions"]),
                "confidence": _group_confidence(events_for_group, review_penalty=0.15 if review_status == "needs_review" else 0.0),
                "review_status": review_status,
                "supporting_mentions": bucket["mentions"],
                "conflicts": [],
                "notes": _people_notes(kind, aliases),
                "metadata": {"normalized_key": key},
            }
        )
    return groups


def _build_place_groups(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        for place in _metadata_list(event, "place_candidates"):
            key = _normalize_key(place)
            if not key:
                continue
            kind = _place_kind(place, event)
            bucket = buckets.setdefault(
                key,
                {
                    "kind": kind,
                    "labels": [],
                    "events": [],
                    "mentions": [],
                },
            )
            if _place_kind_rank(kind) > _place_kind_rank(bucket["kind"]):
                bucket["kind"] = kind
            if place not in bucket["labels"]:
                bucket["labels"].append(place)
            bucket["events"].append(event)
            bucket["mentions"].append(
                {
                    "label": place,
                    "canonical_event_id": event.get("id"),
                    "event_title": event.get("title"),
                    "start_s": event.get("start_s"),
                    "end_s": event.get("end_s"),
                }
            )

    groups = []
    for index, (key, bucket) in enumerate(sorted(buckets.items()), start=1):
        labels = _sorted_labels(bucket["labels"])
        events_for_group = _unique_events(bucket["events"])
        evidence_ids = _event_evidence_ids(events_for_group)
        kind = bucket["kind"]
        review_status = "needs_review" if kind != "named_place_candidate" or len(labels) > 1 else "unreviewed"
        groups.append(
            {
                "id": f"place_group_{index:06d}",
                "kind": kind,
                "label": labels[0],
                "place_type": _place_type(labels[0]),
                "normalized_names": [key],
                "aliases": labels,
                "canonical_event_ids": _event_ids(events_for_group),
                "evidence_ids": evidence_ids,
                "direct_anchor_evidence_ids": [] if kind != "named_place_candidate" else evidence_ids,
                "context_evidence_ids": evidence_ids if kind != "named_place_candidate" else [],
                "source_video_ids": _source_video_ids(evidence_ids, evidence_by_id),
                "first_start_s": _first_start(events_for_group),
                "last_end_s": _last_end(events_for_group),
                "confidence": _group_confidence(events_for_group, review_penalty=0.2 if review_status == "needs_review" else 0.0),
                "review_status": review_status,
                "geocode_candidates": [],
                "not_exportable_as_gps": True,
                "supporting_mentions": bucket["mentions"],
                "notes": _place_notes(kind, labels[0]),
                "metadata": {"broad_place_contexts": sorted(_broad_place_contexts_for_labels(labels))},
            }
        )
    return groups


def _build_date_groups(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        for date_text in _metadata_list(event, "date_candidates"):
            parsed = _parse_date_candidate(date_text)
            key = parsed["date_value"] or _normalize_key(date_text)
            if not key:
                continue
            bucket = buckets.setdefault(
                key,
                {
                    "parsed": parsed,
                    "labels": [],
                    "events": [],
                    "mentions": [],
                },
            )
            if date_text not in bucket["labels"]:
                bucket["labels"].append(date_text)
            bucket["events"].append(event)
            bucket["mentions"].append(
                {
                    "label": date_text,
                    "canonical_event_id": event.get("id"),
                    "event_title": event.get("title"),
                    "start_s": event.get("start_s"),
                    "end_s": event.get("end_s"),
                }
            )

    groups = []
    for index, (key, bucket) in enumerate(sorted(buckets.items()), start=1):
        parsed = bucket["parsed"]
        labels = _sorted_labels(bucket["labels"])
        events_for_group = _unique_events(bucket["events"])
        evidence_ids = _event_evidence_ids(events_for_group)
        excluded = bool(parsed["excluded_as_event_date"])
        review_status = "needs_review" if excluded or len(labels) > 1 else "unreviewed"
        groups.append(
            {
                "id": f"date_group_{index:06d}",
                "label": labels[0],
                "date_value": parsed["date_value"],
                "precision": parsed["precision"],
                "source_kind": parsed["source_kind"],
                "excluded_as_event_date": excluded,
                "canonical_event_ids": _event_ids(events_for_group),
                "evidence_ids": evidence_ids,
                "source_video_ids": _source_video_ids(evidence_ids, evidence_by_id),
                "first_start_s": _first_start(events_for_group),
                "last_end_s": _last_end(events_for_group),
                "confidence": _group_confidence(events_for_group, review_penalty=0.25 if excluded else 0.0),
                "review_status": review_status,
                "supporting_mentions": bucket["mentions"],
                "notes": _date_notes(parsed),
                "metadata": {"normalized_key": key, "aliases": labels},
            }
        )
    return groups


def _build_language_groups(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        for language in _metadata_list(event, "languages"):
            key = _normalize_key(language)
            if not key:
                continue
            bucket = buckets.setdefault(
                key,
                {
                    "language": language,
                    "labels": [],
                    "events": [],
                    "intervals": [],
                },
            )
            if language not in bucket["labels"]:
                bucket["labels"].append(language)
            bucket["events"].append(event)
            bucket["intervals"].append(
                {
                    "canonical_event_id": event.get("id"),
                    "start_s": event.get("start_s"),
                    "end_s": event.get("end_s"),
                }
            )

    groups = []
    for index, (key, bucket) in enumerate(sorted(buckets.items()), start=1):
        events_for_group = _unique_events(bucket["events"])
        evidence_ids = _event_evidence_ids(events_for_group)
        labels = _sorted_labels(bucket["labels"])
        groups.append(
            {
                "id": f"language_group_{index:06d}",
                "language": labels[0],
                "aliases": labels,
                "canonical_event_ids": _event_ids(events_for_group),
                "evidence_ids": evidence_ids,
                "source_video_ids": _source_video_ids(evidence_ids, evidence_by_id),
                "intervals": bucket["intervals"],
                "sample_text": "",
                "translation_available": False,
                "confidence": _group_confidence(events_for_group),
                "review_status": "unreviewed",
                "metadata": {"normalized_key": key},
            }
        )
    return groups


def _build_event_groups(
    events: list[dict[str, Any]],
    date_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = []
    date_contexts = _date_context_event_sets(events, date_groups)
    for date_value, event_set in date_contexts.items():
        if not event_set:
            continue
        events_for_group = [_event_by_id(events, event_id) for event_id in event_set]
        events_for_group = [event for event in events_for_group if event is not None]
        if not events_for_group:
            continue
        label = _format_date_label(date_value)
        groups.append(
            _event_group_record(
                len(groups) + 1,
                title=f"{label} Context",
                group_type="same_day_candidate",
                events=events_for_group,
                confidence=_group_confidence(events_for_group, review_penalty=0.1),
                review_status="needs_review" if _has_context_carried_date(events_for_group, date_value) else "unreviewed",
                merge_reasons=["direct_date", "adjacent_undated_context"],
                split_warnings=["date_context_is_not_final_metadata"],
                metadata={"date_value": date_value},
            )
        )

    broad_contexts = _broad_place_event_sets(events)
    for context, event_set in sorted(broad_contexts.items()):
        if len(event_set) < 2:
            continue
        events_for_group = [_event_by_id(events, event_id) for event_id in event_set]
        events_for_group = [event for event in events_for_group if event is not None]
        title = f"{_title_case(context)} Trip" if _has_event_type(events_for_group, "travel") else f"{_title_case(context)} Context"
        groups.append(
            _event_group_record(
                len(groups) + 1,
                title=title,
                group_type="trip_context" if _has_event_type(events_for_group, "travel") else "place_context",
                events=events_for_group,
                confidence=_group_confidence(events_for_group, review_penalty=0.1),
                review_status="needs_review",
                merge_reasons=["shared_broad_place_context", "contiguous_events"],
                split_warnings=["broad_place_context_should_not_merge_canonical_events"],
                metadata={"broad_place_context": context},
            )
        )

    for segment in _event_type_segments(events):
        if len(segment["events"]) < 2:
            continue
        event_type = segment["event_type"]
        groups.append(
            _event_group_record(
                len(groups) + 1,
                title=f"{_title_case(event_type)} Segment",
                group_type="event_type_segment",
                events=segment["events"],
                confidence=_group_confidence(segment["events"], review_penalty=0.2),
                review_status="needs_review",
                merge_reasons=["contiguous_event_type"],
                split_warnings=["theme_group_only"],
                metadata={"event_type": event_type},
            )
        )
    return groups


def _build_albums(
    events: list[dict[str, Any]],
    event_groups: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    albums = []
    for group in event_groups:
        if group["group_type"] == "same_day_candidate":
            group_events = _events_from_ids(events, group["canonical_event_ids"])
            album_type = "school" if _has_event_type(group_events, "school") else "day"
            title = _album_title_for_day(group, group_events)
            albums.append(_album_record(len(albums) + 1, title, album_type, group_events, group, evidence_by_id))
        elif group["group_type"] == "trip_context":
            group_events = _events_from_ids(events, group["canonical_event_ids"])
            albums.append(_album_record(len(albums) + 1, group["title"], "travel", group_events, group, evidence_by_id))

    covered = {event_id for album in albums for event_id in album["canonical_event_ids"]}
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id or event_id in covered:
            continue
        album_type = _album_type_for_event(event)
        albums.append(
            _album_record(
                len(albums) + 1,
                str(event.get("title") or "Untitled Event"),
                album_type,
                [event],
                None,
                evidence_by_id,
            )
        )
    return _renumber_albums(sorted(albums, key=lambda album: _number_or_large(album.get("start_s"))))


def _event_group_record(
    index: int,
    *,
    title: str,
    group_type: str,
    events: list[dict[str, Any]],
    confidence: float | None,
    review_status: str,
    merge_reasons: list[str],
    split_warnings: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": f"event_group_{index:06d}",
        "title": title,
        "group_type": group_type,
        "canonical_event_ids": _event_ids(events),
        "start_s": _first_start(events),
        "end_s": _last_end(events),
        "evidence_ids": _event_evidence_ids(events),
        "event_types": _most_common_values(_metadata_value(event, "event_type") for event in events),
        "people_labels": _unique_items(person for event in events for person in _metadata_list(event, "people")),
        "place_labels": _unique_items(place for event in events for place in _metadata_list(event, "place_candidates")),
        "date_labels": _safe_date_labels(events),
        "language_labels": _unique_items(language for event in events for language in _metadata_list(event, "languages")),
        "confidence": confidence,
        "review_status": review_status,
        "merge_reasons": merge_reasons,
        "split_warnings": split_warnings,
        "metadata": metadata,
    }


def _album_record(
    index: int,
    title: str,
    album_type: str,
    events: list[dict[str, Any]],
    event_group: dict[str, Any] | None,
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    date_labels = _safe_date_labels(events)
    place_labels = _album_place_labels(events)
    people_labels = _unique_items(person for event in events for person in _metadata_list(event, "people"))
    language_labels = _unique_items(language for event in events for language in _metadata_list(event, "languages"))
    review_status = "needs_review"
    if len(events) == 1 and events[0].get("review_status") == "unreviewed":
        review_status = "unreviewed"
    elif event_group and event_group.get("review_status") == "unreviewed":
        review_status = "unreviewed"
    evidence_ids = _event_evidence_ids(events)
    return {
        "id": f"album_{index:06d}",
        "title": title,
        "album_type": album_type,
        "canonical_event_ids": _event_ids(events),
        "cover_event_id": _cover_event_id(events),
        "date_label": ", ".join(date_labels[:3]),
        "place_label": ", ".join(place_labels[:3]),
        "people_labels": people_labels,
        "language_labels": language_labels,
        "start_s": _first_start(events),
        "end_s": _last_end(events),
        "source_video_ids": _source_video_ids(evidence_ids, evidence_by_id),
        "confidence": _group_confidence(events, review_penalty=0.1 if review_status == "needs_review" else 0.0),
        "review_status": review_status,
        "export_status": "candidate",
        "excluded_event_ids": [],
        "notes": _album_notes(event_group),
        "metadata": {"event_group_id": event_group.get("id") if event_group else None},
    }


def _renumber_albums(albums: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered = []
    for index, album in enumerate(albums, start=1):
        renumbered.append({**album, "id": f"album_{index:06d}"})
    return renumbered


def _date_context_event_sets(
    events: list[dict[str, Any]],
    date_groups: list[dict[str, Any]],
    *,
    carry_gap_s: float = 900.0,
) -> dict[str, list[str]]:
    event_dates: dict[str, set[str]] = defaultdict(set)
    for group in date_groups:
        if group.get("excluded_as_event_date") or group.get("precision") != "day" or not group.get("date_value"):
            continue
        for event_id in group.get("canonical_event_ids", []):
            event_dates[str(event_id)].add(str(group["date_value"]))

    by_id = {event.get("id"): event for event in events if event.get("id")}
    contexts: dict[str, list[str]] = defaultdict(list)
    for index, event in enumerate(events):
        event_id = str(event.get("id") or "")
        for date_value in event_dates.get(event_id, set()):
            selected = {event_id}
            right = index + 1
            while right < len(events):
                candidate = events[right]
                if not _can_carry_date_context(candidate, date_value, event_dates, previous=events[right - 1], carry_gap_s=carry_gap_s):
                    break
                selected.add(str(candidate.get("id")))
                right += 1
            contexts[date_value] = _merge_event_id_lists(contexts[date_value], sorted(selected, key=lambda event_id_value: _event_order(by_id.get(event_id_value))))
    return contexts


def _can_carry_date_context(
    event: dict[str, Any],
    date_value: str,
    event_dates: dict[str, set[str]],
    *,
    previous: dict[str, Any],
    carry_gap_s: float,
) -> bool:
    event_id = str(event.get("id") or "")
    dates = event_dates.get(event_id, set())
    if dates and date_value not in dates:
        return False
    if _gap_seconds(previous, event) > carry_gap_s:
        return False
    return True


def _broad_place_event_sets(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    explicit_contexts_by_event = {
        str(event.get("id")): _broad_place_contexts_for_event(event)
        for event in events
        if event.get("id")
    }
    explicit_indices: dict[str, list[int]] = defaultdict(list)
    for index, event in enumerate(events):
        event_id = str(event.get("id") or "")
        for context in explicit_contexts_by_event.get(event_id, set()):
            explicit_indices[context].append(index)
    ordered = {}
    for context, indices in explicit_indices.items():
        selected: set[str] = set()
        if len(indices) == 1:
            event_id = events[indices[0]].get("id")
            if event_id:
                selected.add(str(event_id))
        else:
            for index in range(min(indices), max(indices) + 1):
                candidate = events[index]
                if not _can_expand_broad_context(candidate, context, explicit_contexts_by_event):
                    continue
                event_id = candidate.get("id")
                if event_id:
                    selected.add(str(event_id))
        ordered[context] = sorted(selected, key=lambda event_id: _event_order(_event_by_id(events, event_id)))
    return ordered


def _can_expand_broad_context(
    event: dict[str, Any],
    context: str,
    explicit_contexts_by_event: dict[str, set[str]],
) -> bool:
    event_id = str(event.get("id") or "")
    contexts = explicit_contexts_by_event.get(event_id, set())
    if contexts and context not in contexts:
        return False
    return _metadata_value(event, "event_type") == "travel"


def _event_type_segments(events: list[dict[str, Any]], *, max_gap_s: float = 900.0) -> list[dict[str, Any]]:
    segments = []
    current: dict[str, Any] | None = None
    for event in events:
        event_type = _metadata_value(event, "event_type")
        if not event_type:
            current = None
            continue
        if (
            current
            and current["event_type"] == event_type
            and _gap_seconds(current["events"][-1], event) <= max_gap_s
        ):
            current["events"].append(event)
        else:
            current = {"event_type": event_type, "events": [event]}
            segments.append(current)
    return segments


def _parse_date_candidate(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    normalized = text.lower()
    month_match = re.search(
        r"\b("
        + "|".join(sorted(DATE_MONTHS, key=len, reverse=True))
        + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})\b",
        normalized,
    )
    if month_match:
        month = DATE_MONTHS[month_match.group(1).rstrip(".")]
        day = int(month_match.group(2))
        year = int(month_match.group(3))
        date_value = f"{year:04d}-{month:02d}-{day:02d}"
        return {
            "date_value": date_value,
            "precision": "day",
            "source_kind": "event_date_candidate",
            "excluded_as_event_date": False,
        }

    year_match = re.fullmatch(r".*?\b(18\d{2}|19\d{2}|20\d{2})\b.*", normalized)
    if year_match:
        return {
            "date_value": year_match.group(1),
            "precision": "year",
            "source_kind": "mentioned_historical_date",
            "excluded_as_event_date": True,
        }

    decade_match = re.search(r"\b(?:early|mid|late)?-?\s*(\d{2})s\b", normalized)
    if decade_match:
        decade = int(decade_match.group(1))
        century = 1900 if decade >= 30 else 2000
        return {
            "date_value": f"{century + decade:04d}s",
            "precision": "decade",
            "source_kind": "mentioned_historical_date",
            "excluded_as_event_date": True,
        }

    return {
        "date_value": None,
        "precision": "unknown",
        "source_kind": "date_candidate",
        "excluded_as_event_date": True,
    }


def _person_key_and_kind(value: str) -> tuple[str, str]:
    normalized = _normalize_key(value)
    tokens = set(normalized.split())
    if normalized in ROLE_PERSON_TOKENS or tokens & ROLE_PERSON_TOKENS:
        return normalized, "role_candidate"
    return PERSON_ALIAS_KEYS.get(normalized, normalized), "person_candidate"


def _place_kind(value: str, event: dict[str, Any]) -> str:
    normalized = _normalize_key(value)
    event_type = _metadata_value(event, "event_type")
    if event_type == "home" and _is_mentioned_place_key(normalized):
        return "mentioned_place_candidate"
    if _is_generic_place_key(normalized):
        return "generic_place_context"
    return "named_place_candidate"


def _place_kind_rank(kind: str) -> int:
    ranks = {
        "generic_place_context": 1,
        "mentioned_place_candidate": 2,
        "named_place_candidate": 3,
    }
    return ranks.get(kind, 0)


def _place_type(label: str) -> str:
    key = _normalize_key(label)
    if "school" in key or "classroom" in key:
        return "school"
    if _is_room_place_key(key):
        return "room"
    if "volcano" in key or key in {"hualalai", "kilauea"}:
        return "volcano"
    if "garden" in key or "rainforest" in key or "waterfall" in key:
        return "nature"
    if "ocean" in key or "beach" in key or "hot springs" in key:
        return "water"
    if "restaurant" in key:
        return "restaurant"
    if key in {"hawaii", "oregon", "moscow"} or "," in str(label):
        return "region"
    return "place_candidate"


def _broad_place_contexts_for_event(event: dict[str, Any]) -> set[str]:
    return _broad_place_contexts_for_labels(_metadata_list(event, "place_candidates"))


def _broad_place_contexts_for_labels(labels: list[str]) -> set[str]:
    contexts = set()
    normalized_labels = {_normalize_key(label) for label in labels}
    for context, aliases in BROAD_PLACE_ALIASES.items():
        if normalized_labels & aliases:
            contexts.add(context)
            continue
        for label in normalized_labels:
            if any(alias in label for alias in aliases):
                contexts.add(context)
                break
    return contexts


def _metadata_value(event: dict[str, Any], key: str) -> Any:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return metadata.get(key) or event.get(key)


def _metadata_list(event: dict[str, Any], key: str) -> list[str]:
    value = _metadata_value(event, key)
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _source_video_ids(evidence_ids: list[str], evidence_by_id: dict[str, dict[str, Any]]) -> list[str]:
    return _unique_items(evidence_by_id.get(evidence_id, {}).get("source_video_id") for evidence_id in evidence_ids)


def _event_evidence_ids(events: list[dict[str, Any]]) -> list[str]:
    return _unique_items(evidence_id for event in events for evidence_id in event.get("evidence_ids", []))


def _event_ids(events: list[dict[str, Any]]) -> list[str]:
    return _unique_items(event.get("id") for event in sorted(events, key=lambda row: _number_or_large(row.get("start_s"))))


def _unique_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for event in sorted(events, key=lambda row: _number_or_large(row.get("start_s"))):
        event_id = event.get("id") or id(event)
        if event_id in seen:
            continue
        seen.add(event_id)
        unique.append(event)
    return unique


def _unique_items(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _sorted_labels(values: list[str]) -> list[str]:
    return sorted(_unique_items(values), key=lambda value: (value.casefold(), value))


def _most_common_values(values: Any) -> list[str]:
    cleaned = [str(value) for value in values if value not in (None, "")]
    return [item for item, _ in Counter(cleaned).most_common()]


def _first_start(events: list[dict[str, Any]]) -> float | None:
    starts = [_number_or_none(event.get("start_s")) for event in events]
    starts = [start for start in starts if start is not None]
    return round(min(starts), 3) if starts else None


def _last_end(events: list[dict[str, Any]]) -> float | None:
    ends = [_number_or_none(event.get("end_s")) for event in events]
    ends = [end for end in ends if end is not None]
    return round(max(ends), 3) if ends else None


def _group_confidence(events: list[dict[str, Any]], *, review_penalty: float = 0.0) -> float | None:
    values = [float(event["confidence"]) for event in events if isinstance(event.get("confidence"), (int, float))]
    if not values:
        return None
    return round(max(0.0, min(1.0, (sum(values) / len(values)) - review_penalty)), 3)


def _people_notes(kind: str, aliases: list[str]) -> list[str]:
    notes = []
    if kind == "role_candidate":
        notes.append("Role/group mention, not a confirmed identity.")
    if len(aliases) > 1:
        notes.append("Alias grouping is deterministic and needs human review.")
    return notes


def _place_notes(kind: str, label: str) -> list[str]:
    if kind == "generic_place_context":
        return ["Generic visual/place context, not an exact location."]
    if kind == "mentioned_place_candidate":
        return ["May be mentioned in narration or decor context rather than filming location."]
    return ["Candidate place only; geocoding/export requires confirmation."]


def _date_notes(parsed: dict[str, Any]) -> list[str]:
    if parsed["excluded_as_event_date"]:
        return ["Mentioned or ambiguous date; not safe as an event recording date."]
    return ["Candidate recording/event date; export requires review or stronger direct evidence."]


def _album_notes(event_group: dict[str, Any] | None) -> list[str]:
    if not event_group:
        return []
    if event_group.get("group_type") == "same_day_candidate":
        return ["Album may include adjacent undated context; review before metadata export."]
    if event_group.get("group_type") == "trip_context":
        return ["Trip album keeps sub-events separate; broad place context is not GPS metadata."]
    return []


def _events_from_ids(events: list[dict[str, Any]], event_ids: list[str]) -> list[dict[str, Any]]:
    by_id = {event.get("id"): event for event in events}
    return [by_id[event_id] for event_id in event_ids if event_id in by_id]


def _event_by_id(events: list[dict[str, Any]], event_id: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("id") == event_id:
            return event
    return None


def _event_order(event: dict[str, Any] | None) -> float:
    if not event:
        return 1_000_000_000.0
    return _number_or_large(event.get("start_s"))


def _merge_event_id_lists(left: list[str], right: list[str]) -> list[str]:
    return _unique_items([*left, *right])


def _has_context_carried_date(events: list[dict[str, Any]], date_value: str) -> bool:
    for event in events:
        parsed_dates = [_parse_date_candidate(date) for date in _metadata_list(event, "date_candidates")]
        direct_values = {parsed["date_value"] for parsed in parsed_dates if not parsed["excluded_as_event_date"]}
        if not direct_values or date_value not in direct_values:
            return True
    return False


def _has_event_type(events: list[dict[str, Any]], event_type: str) -> bool:
    return any(_metadata_value(event, "event_type") == event_type for event in events)


def _album_title_for_day(group: dict[str, Any], events: list[dict[str, Any]]) -> str:
    date_value = group.get("metadata", {}).get("date_value")
    date_label = _format_date_label(date_value) if date_value else group.get("title", "Day")
    place = _album_place_label(events)
    if _has_event_type(events, "school") and place:
        if "school" in _normalize_key(place):
            return f"{place} Day, {date_label}"
        return f"{place} School Day, {date_label}"
    if place:
        return f"{place}, {date_label}"
    return str(date_label)


def _album_place_label(events: list[dict[str, Any]]) -> str:
    place_labels = _album_place_labels(events)
    if not place_labels:
        return ""
    return place_labels[0]


def _album_place_labels(events: list[dict[str, Any]]) -> list[str]:
    place_counter: Counter[str] = Counter()
    generic_counter: Counter[str] = Counter()
    first_seen: dict[str, str] = {}
    first_order: dict[str, int] = {}
    order = 0
    for event in events:
        for place in _metadata_list(event, "place_candidates"):
            key = _normalize_key(place)
            if _is_mentioned_place_key(key):
                continue
            if key not in first_order:
                first_order[key] = order
                order += 1
            first_seen.setdefault(key, place)
            if _is_generic_place_key(key):
                if not _is_room_place_key(key):
                    generic_counter[key] += 1
            else:
                place_counter[key] += 1
    counter = place_counter or generic_counter
    ranked = sorted(
        counter,
        key=lambda key: (-counter[key], _album_place_priority(first_seen[key]), first_order[key]),
    )
    return [first_seen[key] for key in ranked]


def _safe_date_labels(events: list[dict[str, Any]]) -> list[str]:
    labels = []
    seen_values = set()
    for event in events:
        for date_text in _metadata_list(event, "date_candidates"):
            parsed = _parse_date_candidate(date_text)
            if parsed["excluded_as_event_date"] or not parsed["date_value"]:
                continue
            if parsed["date_value"] in seen_values:
                continue
            seen_values.add(parsed["date_value"])
            labels.append(date_text)
    return labels


def _album_type_for_event(event: dict[str, Any]) -> str:
    event_type = _metadata_value(event, "event_type")
    if event_type in {"school", "home", "medical", "travel"}:
        return str(event_type)
    title = _normalize_key(event.get("title"))
    if "zoo" in title or "safari" in title:
        return "animals"
    return "unreviewed"


def _cover_event_id(events: list[dict[str, Any]]) -> str | None:
    if not events:
        return None
    best = max(events, key=lambda event: (float(event.get("confidence") or 0.0), _duration(event)))
    return best.get("id")


def _duration(event: dict[str, Any]) -> float:
    start = _number_or_none(event.get("start_s")) or 0.0
    end = _number_or_none(event.get("end_s")) or start
    return max(0.0, end - start)


def _gap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_end = _number_or_none(left.get("end_s")) or _number_or_none(left.get("start_s")) or 0.0
    right_start = _number_or_none(right.get("start_s")) or _number_or_none(right.get("end_s")) or 0.0
    return right_start - left_end


def _format_date_label(date_value: Any) -> str:
    if not date_value:
        return "Undated"
    text = str(date_value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    except ValueError:
        return text


def _title_case(value: Any) -> str:
    return str(value or "").replace("_", " ").title()


def _normalize_key(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_room_place_key(key: str) -> bool:
    return key in {_normalize_key(value) for value in ROOM_PLACE_TOKENS}


def _is_generic_place_key(key: str) -> bool:
    return key in {_normalize_key(value) for value in GENERIC_PLACE_TOKENS}


def _is_mentioned_place_key(key: str) -> bool:
    return key in {_normalize_key(value) for value in MENTIONED_PLACE_CONTEXT_TOKENS}


def _album_place_priority(label: str) -> int:
    key = _normalize_key(label)
    if "school" in key or "spencer butte" in key:
        return 0
    if key in {"hilo", "hualalai", "kilauea", "kilauea volcano"}:
        return 1
    if key in {"hawaii", "oregon", "moscow"} or "," in str(label):
        return 2
    return 3


def _number_or_large(value: Any) -> float:
    number = _number_or_none(value)
    return number if number is not None else 1_000_000_000.0


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    path.touch()
    for row in rows:
        append_jsonl(path, row)

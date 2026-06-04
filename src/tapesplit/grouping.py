from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import json
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

PERSON_ALIAS_FILENAMES = ("person_aliases.json", "people_aliases.json")

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
    "apartment",
    "apartment complex",
    "beach",
    "botanical garden",
    "chapel",
    "community garden",
    "community gardens",
    "crater",
    "garden",
    "gym",
    "hall",
    "hot springs",
    "indoor hall",
    "indoor hall gym",
    "kindergarten",
    "lake",
    "library",
    "ocean",
    "outdoor snowy area",
    "park",
    "playground",
    "rainforest",
    "restaurant",
    "school",
    "school hall",
    "street",
    "sulfur springs",
    "volcano",
    "waterfalls",
}

US_STATE_KEYS = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
}

COUNTRY_KEYS = {
    "canada",
    "mexico",
    "russia",
    "ukraine",
    "united states",
    "usa",
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
    context_events = [event for event in events if _is_contextual_event(event)]
    evidence = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in evidence if row.get("id")}
    person_alias_keys = _person_alias_keys(project)

    people_groups = _build_people_groups(context_events, evidence_by_id, person_alias_keys)
    place_groups = _build_place_groups(context_events, evidence_by_id)
    date_groups = _build_date_groups(context_events, evidence_by_id)
    language_groups = _build_language_groups(context_events, evidence_by_id)
    event_groups = _build_event_groups(context_events, date_groups)
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
        "context_events": len(context_events),
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
    person_alias_keys: dict[str, str],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        for person in _metadata_list(event, "people"):
            key, kind = _person_key_and_kind(person, person_alias_keys)
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
    place_aliases = _place_admin_alias_resolution(
        place for event in events for place in _metadata_list(event, "place_candidates")
    )
    event_contexts = _place_contexts_by_event(events, evidence_by_id, place_aliases)
    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        for place in _metadata_list(event, "place_candidates"):
            normalized = _normalize_key(place)
            if not normalized:
                continue
            kind = _place_kind(place, event)
            key = _place_resolution_key(place, kind, event, event_contexts, place_aliases)
            bucket = buckets.setdefault(
                key,
                {
                    "kind": kind,
                    "labels": [],
                    "normalized_names": [],
                    "events": [],
                    "mentions": [],
                },
            )
            if _place_kind_rank(kind) > _place_kind_rank(bucket["kind"]):
                bucket["kind"] = kind
            if place not in bucket["labels"]:
                bucket["labels"].append(place)
            if normalized not in bucket["normalized_names"]:
                bucket["normalized_names"].append(normalized)
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
        label = _preferred_place_label(labels)
        events_for_group = _unique_events(bucket["events"])
        evidence_ids = _event_evidence_ids(events_for_group)
        kind = bucket["kind"]
        review_status = "needs_review" if kind != "named_place_candidate" or len(labels) > 1 else "unreviewed"
        groups.append(
            {
                "id": f"place_group_{index:06d}",
                "kind": kind,
                "label": label,
                "place_type": _place_type(label),
                "normalized_names": _sorted_labels(bucket["normalized_names"]),
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
                "scope_label": _place_scope_label(events_for_group, evidence_by_id, event_contexts),
                "parent_place_labels": [],
                "nearby_place_labels": [],
                "supporting_mentions": bucket["mentions"],
                "notes": _place_notes(kind, label),
                "metadata": {
                    "resolution_key": key,
                    "broad_place_contexts": sorted(_broad_place_contexts_for_labels(labels)),
                    "scope": _place_scope_metadata(events_for_group, evidence_by_id, event_contexts),
                    "parent_place_candidates": [],
                    "nearby_place_candidates": [],
                },
            }
        )
    return _attach_place_context_candidates(groups, events, evidence_by_id, event_contexts)


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
    excluded = album_type in {"unrelated_content", "non_content"}
    review_status = "needs_review"
    if excluded:
        review_status = "excluded"
    elif len(events) == 1 and events[0].get("review_status") == "unreviewed":
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
        "export_status": "excluded" if excluded else "candidate",
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


def _person_key_and_kind(value: str, person_alias_keys: dict[str, str]) -> tuple[str, str]:
    normalized = _normalize_key(value)
    tokens = set(normalized.split())
    if normalized in ROLE_PERSON_TOKENS or tokens & ROLE_PERSON_TOKENS:
        return normalized, "role_candidate"
    spelling_key = _person_spelling_key(normalized)
    return person_alias_keys.get(normalized) or person_alias_keys.get(spelling_key) or spelling_key, "person_candidate"


def _person_alias_keys(project: Path) -> dict[str, str]:
    groups = _load_project_person_alias_groups(project)
    keys: dict[str, str] = {}
    for group in groups:
        labels = [str(label) for label in group if str(label).strip()]
        if len(labels) < 2:
            continue
        canonical = _person_spelling_key(_normalize_key(labels[0]))
        for label in labels:
            normalized = _normalize_key(label)
            if not normalized:
                continue
            keys.setdefault(normalized, canonical)
            keys.setdefault(_person_spelling_key(normalized), canonical)
    return keys


def _load_project_person_alias_groups(project: Path) -> list[tuple[str, ...]]:
    groups = []
    for filename in PERSON_ALIAS_FILENAMES:
        path = project / filename
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("aliases") or payload.get("groups") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            continue
        for item in items:
            labels = _alias_group_labels(item)
            if len(labels) >= 2:
                groups.append(tuple(labels))
    return groups


def _alias_group_labels(item: Any) -> list[str]:
    if isinstance(item, list):
        return [str(label) for label in item if str(label).strip()]
    if not isinstance(item, dict):
        return []
    labels = []
    for key in ["canonical", "name", "label", "key"]:
        if item.get(key):
            labels.append(str(item[key]))
            break
    aliases = item.get("aliases") or item.get("names") or []
    if isinstance(aliases, list):
        labels.extend(str(alias) for alias in aliases if str(alias).strip())
    return labels


def _person_spelling_key(value: str) -> str:
    tokens = []
    for token in value.split():
        if re.fullmatch(r"[a-z]+", token):
            token = token.replace("ph", "f")
            token = re.sub(r"([bcdfghjklmnpqrstvwxyz])\1+", r"\1", token)
            if len(token) > 3 and token.endswith("ie"):
                token = token[:-2] + "y"
        tokens.append(token)
    return " ".join(tokens)


def _place_admin_alias_resolution(labels: Any) -> dict[str, Any]:
    label_list = _unique_items(labels)
    qualified_by_base: dict[str, list[str]] = defaultdict(list)
    for label in label_list:
        parts = _comma_admin_parts(label)
        if len(parts) >= 2:
            qualified_by_base[parts[0]].append(label)

    alias_to_key: dict[str, str] = {}
    key_to_label: dict[str, str] = {}
    key_to_labels: dict[str, list[str]] = defaultdict(list)
    for base, qualified_labels in qualified_by_base.items():
        signatures = [_comma_admin_parts(label)[1:] for label in qualified_labels]
        if not _admin_signatures_compatible(signatures):
            continue
        canonical_label = _preferred_admin_label(qualified_labels)
        key = f"admin:{_normalize_key(canonical_label)}"
        key_to_label[key] = canonical_label
        for label in qualified_labels:
            normalized = _normalize_key(label)
            alias_to_key[normalized] = key
            if label not in key_to_labels[key]:
                key_to_labels[key].append(label)
        for label in label_list:
            if _normalize_key(label) == base:
                alias_to_key[base] = key
                if label not in key_to_labels[key]:
                    key_to_labels[key].append(label)
        alias_to_key.setdefault(base, key)

    return {
        "alias_to_key": alias_to_key,
        "key_to_label": key_to_label,
        "key_to_labels": {key: _sorted_labels(values) for key, values in key_to_labels.items()},
    }


def _comma_admin_parts(label: Any) -> list[str]:
    text = str(label or "")
    if "," not in text:
        return []
    return [_normalize_key(part) for part in text.split(",") if _normalize_key(part)]


def _admin_signatures_compatible(signatures: list[list[str]]) -> bool:
    cleaned = [signature for signature in signatures if signature]
    for index, left in enumerate(cleaned):
        for right in cleaned[index + 1 :]:
            if not (_is_suffix(left, right) or _is_suffix(right, left)):
                return False
    return True


def _is_suffix(shorter: list[str], longer: list[str]) -> bool:
    if len(shorter) > len(longer):
        return False
    return shorter == longer[-len(shorter) :]


def _preferred_admin_label(labels: list[str]) -> str:
    return sorted(
        labels,
        key=lambda label: (-len(_comma_admin_parts(label)), -len(_normalize_key(label)), str(label).casefold()),
    )[0]


def _preferred_place_label(labels: list[str]) -> str:
    return sorted(
        labels,
        key=lambda label: (-_place_label_specificity(label), str(label).casefold(), str(label)),
    )[0]


def _place_label_specificity(label: str) -> int:
    parts = _comma_admin_parts(label)
    if parts:
        return 100 + len(parts)
    key = _normalize_key(label)
    if _is_region_place_label(label):
        return 80
    if _is_generic_place_key(key):
        return 10
    return 50 + len(key.split())


def _place_resolution_key(
    place: str,
    kind: str,
    event: dict[str, Any],
    event_contexts: dict[str, dict[str, Any]],
    place_aliases: dict[str, Any],
) -> str:
    admin_key = _admin_key_for_label(place, place_aliases)
    if admin_key:
        return admin_key
    normalized = _normalize_key(place)
    if kind == "generic_place_context":
        event_id = str(event.get("id") or "")
        context = event_contexts.get(event_id, {})
        admin_keys = [str(key) for key in context.get("admin_keys") or []]
        if admin_keys and not _event_is_multiplace_compilation(event):
            return f"{normalized}|{'+'.join(admin_keys)}"
        source_key = "+".join(str(item) for item in context.get("source_video_ids") or []) or "unknown_source"
        return f"{normalized}|source:{source_key}"
    return normalized


def _place_contexts_by_event(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    place_aliases: dict[str, Any],
    *,
    nearby_gap_s: float = 1800.0,
) -> dict[str, dict[str, Any]]:
    base_contexts: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        source_video_ids = _event_source_video_ids(event, evidence_by_id)
        direct_admin_keys = [] if _event_is_multiplace_compilation(event) else _admin_keys_for_event(event, place_aliases)
        base_contexts[event_id] = {
            "source_video_ids": source_video_ids,
            "direct_admin_keys": direct_admin_keys,
            "nearby_admin_keys": [],
        }

    event_index_by_id = {str(event.get("id")): index for index, event in enumerate(events) if event.get("id")}
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        if _event_is_multiplace_compilation(event):
            base_contexts[event_id]["nearby_admin_keys"] = []
            base_contexts[event_id]["admin_keys"] = base_contexts[event_id]["direct_admin_keys"]
            base_contexts[event_id]["admin_labels"] = [_admin_label_for_key(key, place_aliases) for key in base_contexts[event_id]["admin_keys"]]
            continue
        source_video_ids = set(base_contexts[event_id]["source_video_ids"])
        nearby_keys: list[str] = []
        for other in events:
            other_id = str(other.get("id") or "")
            if not other_id or other_id == event_id:
                continue
            if not _can_carry_place_context_between(events, event_index_by_id[event_id], event_index_by_id[other_id]):
                continue
            if source_video_ids and not (source_video_ids & set(base_contexts[other_id]["source_video_ids"])):
                continue
            if _event_gap_seconds(event, other) > nearby_gap_s:
                continue
            for key in base_contexts[other_id]["direct_admin_keys"]:
                if key not in nearby_keys:
                    nearby_keys.append(key)
        base_contexts[event_id]["nearby_admin_keys"] = nearby_keys
        admin_keys = _unique_items([*base_contexts[event_id]["direct_admin_keys"], *nearby_keys])
        base_contexts[event_id]["admin_keys"] = admin_keys
        base_contexts[event_id]["admin_labels"] = [_admin_label_for_key(key, place_aliases) for key in admin_keys]
    return base_contexts


def _can_carry_place_context_between(events: list[dict[str, Any]], left_index: int, right_index: int) -> bool:
    left = events[left_index]
    right = events[right_index]
    if _event_is_multiplace_compilation(left) or _event_is_multiplace_compilation(right):
        return False
    left_type = str(_metadata_value(left, "event_type") or "")
    right_type = str(_metadata_value(right, "event_type") or "")
    if "travel" in {left_type, right_type} and left_type != right_type:
        return False
    if {left_type, right_type} <= {"school", "holiday"}:
        return True
    if left_type and right_type and left_type != right_type:
        return False
    start = min(left_index, right_index)
    end = max(left_index, right_index)
    for boundary in events[start + 1 : end]:
        boundary_type = str(_metadata_value(boundary, "event_type") or "")
        if _event_is_multiplace_compilation(boundary):
            return False
        if boundary_type == "travel" and left_type != "travel":
            return False
    return True


def _admin_keys_for_event(event: dict[str, Any], place_aliases: dict[str, Any]) -> list[str]:
    keys = []
    for place in _metadata_list(event, "place_candidates"):
        key = _admin_key_for_label(place, place_aliases)
        if key and key not in keys:
            keys.append(key)
    return keys


def _admin_key_for_label(label: Any, place_aliases: dict[str, Any]) -> str:
    normalized = _normalize_key(label)
    alias_key = place_aliases.get("alias_to_key", {}).get(normalized)
    if alias_key:
        return str(alias_key)
    if _is_region_place_label(label):
        return f"region:{normalized}"
    return ""


def _admin_label_for_key(key: str, place_aliases: dict[str, Any]) -> str:
    if key in place_aliases.get("key_to_label", {}):
        return str(place_aliases["key_to_label"][key])
    if key.startswith("region:"):
        return _title_case(key.removeprefix("region:"))
    return key


def _event_source_video_ids(
    event: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    source_ids = event.get("source_video_ids")
    if isinstance(source_ids, list):
        return [str(item) for item in source_ids if item]
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_ids = metadata.get("source_video_ids")
    if isinstance(source_ids, list):
        return [str(item) for item in source_ids if item]
    from_evidence = _source_video_ids(_event_evidence_ids([event]), evidence_by_id)
    if from_evidence:
        return from_evidence
    return [str(event.get("source_video_id"))] if event.get("source_video_id") else []


def _event_is_multiplace_compilation(event: dict[str, Any]) -> bool:
    return len(_metadata_list(event, "place_candidates")) >= 4 and len(_metadata_list(event, "date_candidates")) >= 3


def _place_scope_label(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    event_contexts: dict[str, dict[str, Any]],
) -> str:
    scope = _place_scope_metadata(events, evidence_by_id, event_contexts)
    admin_labels = scope.get("admin_context_labels") or []
    if admin_labels:
        return f"{', '.join(admin_labels[:2])} context"
    parts = []
    source_video_ids = scope.get("source_video_ids") or []
    if source_video_ids:
        parts.append("+".join(source_video_ids[:2]))
    years = scope.get("date_years") or []
    if len(years) == 1:
        parts.append(str(years[0]))
    elif len(years) > 1:
        parts.append(f"{years[0]}-{years[-1]}")
    return f"{', '.join(parts)} context" if parts else ""


def _place_scope_metadata(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    event_contexts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    event_ids = _event_ids(events)
    contexts = [event_contexts.get(event_id, {}) for event_id in event_ids]
    source_video_ids = _unique_items(
        source_video_id
        for event in events
        for source_video_id in _event_source_video_ids(event, evidence_by_id)
    )
    admin_keys = _unique_items(key for context in contexts for key in context.get("admin_keys", []))
    admin_labels = _unique_items(label for context in contexts for label in context.get("admin_labels", []))
    return {
        "source_video_ids": source_video_ids,
        "date_years": _date_years_for_events(events),
        "admin_context_keys": admin_keys,
        "admin_context_labels": admin_labels,
    }


def _attach_place_context_candidates(
    groups: list[dict[str, Any]],
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    event_contexts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events_by_id = {str(event.get("id")): event for event in events if event.get("id")}
    groups_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    groups_by_resolution_key: dict[str, dict[str, Any]] = {}
    for group in groups:
        resolution_key = str(group.get("metadata", {}).get("resolution_key") or "")
        if resolution_key:
            groups_by_resolution_key[resolution_key] = group
        for event_id in group.get("canonical_event_ids") or []:
            groups_by_event[str(event_id)].append(group)

    for group in groups:
        parent_candidates: dict[tuple[str, str], dict[str, Any]] = {}
        nearby_candidates: dict[tuple[str, str], dict[str, Any]] = {}
        group_events = _events_from_ids(events, group.get("canonical_event_ids") or [])

        for event in group_events:
            event_id = str(event.get("id") or "")
            context = event_contexts.get(event_id, {})
            for admin_key in context.get("admin_keys") or []:
                parent = groups_by_resolution_key.get(str(admin_key))
                if parent and parent.get("id") != group.get("id") and _can_attach_admin_parent(parent, group):
                    relation = "within_region_candidate"
                    basis = "direct_admin_context" if admin_key in (context.get("direct_admin_keys") or []) else "nearby_admin_context"
                    confidence = 0.74 if basis == "direct_admin_context" else 0.56
                    _add_place_context_candidate(parent_candidates, parent, relation, confidence, basis, [event_id])

            if not _event_is_multiplace_compilation(event):
                for other in groups_by_event.get(event_id, []):
                    if other.get("id") == group.get("id"):
                        continue
                    if _is_parent_place_candidate(other, group):
                        _add_place_context_candidate(
                            parent_candidates,
                            other,
                            _place_parent_relation(other),
                            0.7,
                            "same_event_place_context",
                            [event_id],
                        )
                    elif _is_nearby_place_candidate(group, other):
                        _add_place_context_candidate(
                            nearby_candidates,
                            other,
                            "same_event_place_context",
                            0.62,
                            "same_event_place_context",
                            [event_id],
                        )

        for other in groups:
            if other.get("id") == group.get("id"):
                continue
            gap = _place_group_gap_seconds(group, other)
            if gap > 900.0 or not _place_groups_share_source(group, other):
                continue
            if not _place_groups_have_compatible_event_context(group, other, events_by_id):
                continue
            common_events = _merge_event_id_lists(group.get("canonical_event_ids") or [], other.get("canonical_event_ids") or [])
            if _is_parent_place_candidate(other, group):
                _add_place_context_candidate(
                    parent_candidates,
                    other,
                    _place_parent_relation(other),
                    0.52,
                    "nearby_time_place_context",
                    common_events,
                )
            elif _is_nearby_place_candidate(group, other):
                _add_place_context_candidate(
                    nearby_candidates,
                    other,
                    "nearby_time_place_context",
                    0.48,
                    "nearby_time_place_context",
                    common_events,
                )

        parent_list = _place_context_candidate_list(parent_candidates)
        nearby_list = _place_context_candidate_list(nearby_candidates)
        group["parent_place_labels"] = _unique_items(candidate["label"] for candidate in parent_list[:6])
        group["nearby_place_labels"] = _unique_items(candidate["label"] for candidate in nearby_list[:6])
        group["metadata"]["parent_place_candidates"] = parent_list
        group["metadata"]["nearby_place_candidates"] = nearby_list
        if parent_list:
            group["scope_label"] = f"{', '.join(group['parent_place_labels'][:2])} context"
        elif group.get("kind") == "named_place_candidate" and group.get("place_type") != "region":
            group["scope_label"] = _place_source_scope_label(group.get("metadata", {}).get("scope", {}))
            group["metadata"]["scope"]["admin_context_keys"] = []
            group["metadata"]["scope"]["admin_context_labels"] = []
        if parent_list:
            group["notes"] = _unique_items([*group.get("notes", []), "Has reviewable broader-place context; not exportable as GPS until confirmed."])
        if nearby_list:
            group["notes"] = _unique_items([*group.get("notes", []), "Has reviewable same-area/nearby-place context."])

    return groups


def _place_source_scope_label(scope: dict[str, Any]) -> str:
    parts = []
    source_video_ids = [str(item) for item in scope.get("source_video_ids") or [] if item]
    if source_video_ids:
        parts.append("+".join(source_video_ids[:2]))
    years = [str(item) for item in scope.get("date_years") or [] if item]
    if len(years) == 1:
        parts.append(years[0])
    elif len(years) > 1:
        parts.append(f"{years[0]}-{years[-1]}")
    return f"{', '.join(parts)} context" if parts else ""


def _add_place_context_candidate(
    candidates: dict[tuple[str, str], dict[str, Any]],
    place: dict[str, Any],
    relation: str,
    confidence: float,
    basis: str,
    event_ids: list[str],
) -> None:
    key = (str(place.get("id")), relation)
    candidate = candidates.setdefault(
        key,
        {
            "place_group_id": place.get("id"),
            "label": place.get("label"),
            "relation": relation,
            "confidence": confidence,
            "review_status": "needs_review",
            "canonical_event_ids": [],
            "basis": [],
            "not_exportable_as_gps": True,
        },
    )
    candidate["confidence"] = round(max(float(candidate["confidence"]), confidence), 3)
    candidate["canonical_event_ids"] = _merge_event_id_lists(candidate["canonical_event_ids"], event_ids)
    if basis not in candidate["basis"]:
        candidate["basis"].append(basis)


def _place_context_candidate_list(candidates: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates.values(),
        key=lambda candidate: (-float(candidate.get("confidence") or 0.0), str(candidate.get("label") or "").casefold()),
    )


def _is_parent_place_candidate(candidate: dict[str, Any], group: dict[str, Any]) -> bool:
    if candidate.get("place_type") == "region":
        return group.get("place_type") != "region" and _can_attach_admin_parent(candidate, group)
    candidate_key = _normalize_key(candidate.get("label"))
    group_key = _normalize_key(group.get("label"))
    if "school" in candidate_key and any(token in group_key for token in ["classroom", "gym", "hall"]):
        return True
    return False


def _can_attach_admin_parent(parent: dict[str, Any], group: dict[str, Any]) -> bool:
    if group.get("kind") == "generic_place_context":
        return True
    if group.get("place_type") in {"school", "restaurant", "room", "park", "water", "nature"}:
        return True
    group_key = _normalize_key(group.get("label"))
    parent_key = _normalize_key(parent.get("label"))
    if parent_key in US_STATE_KEYS or parent_key in COUNTRY_KEYS:
        return True
    if len(group_key.split()) >= 2:
        return True
    return False


def _place_parent_relation(candidate: dict[str, Any]) -> str:
    if candidate.get("place_type") == "region":
        return "within_region_candidate"
    return "inside_place_candidate"


def _is_nearby_place_candidate(group: dict[str, Any], other: dict[str, Any]) -> bool:
    if group.get("place_type") == "region" or other.get("place_type") == "region":
        return False
    if group.get("kind") == "mentioned_place_candidate" or other.get("kind") == "mentioned_place_candidate":
        return False
    return True


def _place_group_gap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_start = _number_or_none(left.get("first_start_s"))
    left_end = _number_or_none(left.get("last_end_s")) or left_start
    right_start = _number_or_none(right.get("first_start_s"))
    right_end = _number_or_none(right.get("last_end_s")) or right_start
    if None in {left_start, left_end, right_start, right_end}:
        return 1_000_000_000.0
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0.0


def _place_groups_share_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_sources = set(left.get("source_video_ids") or [])
    right_sources = set(right.get("source_video_ids") or [])
    if not left_sources or not right_sources:
        return False
    return bool(left_sources & right_sources)


def _place_groups_have_compatible_event_context(
    left: dict[str, Any],
    right: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
) -> bool:
    left_events = _events_from_group_row(left, events_by_id)
    right_events = _events_from_group_row(right, events_by_id)
    if not left_events or not right_events:
        return False
    if any(_event_is_multiplace_compilation(event) for event in [*left_events, *right_events]):
        return False
    left_types = {str(_metadata_value(event, "event_type") or "") for event in left_events}
    right_types = {str(_metadata_value(event, "event_type") or "") for event in right_events}
    if "travel" in left_types ^ right_types:
        return False
    if left_types <= {"school", "holiday"} and right_types <= {"school", "holiday"}:
        return True
    known_left = {event_type for event_type in left_types if event_type}
    known_right = {event_type for event_type in right_types if event_type}
    return not known_left or not known_right or bool(known_left & known_right)


def _events_from_group_row(group: dict[str, Any], events_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [events_by_id[event_id] for event_id in group.get("canonical_event_ids") or [] if event_id in events_by_id]


def _event_gap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_start = _number_or_none(left.get("start_s"))
    left_end = _number_or_none(left.get("end_s")) or left_start
    right_start = _number_or_none(right.get("start_s"))
    right_end = _number_or_none(right.get("end_s")) or right_start
    if None in {left_start, left_end, right_start, right_end}:
        return 1_000_000_000.0
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0.0


def _date_years_for_events(events: list[dict[str, Any]]) -> list[str]:
    years = []
    for event in events:
        for date_text in _metadata_list(event, "date_candidates"):
            parsed = _parse_date_candidate(date_text)
            if parsed["excluded_as_event_date"] or not parsed["date_value"]:
                continue
            year = str(parsed["date_value"])[:4]
            if year not in years:
                years.append(year)
    return sorted(years)


def _is_region_place_label(label: Any) -> bool:
    key = _normalize_key(label)
    return "," in str(label or "") or key in US_STATE_KEYS or key in COUNTRY_KEYS or key in BROAD_PLACE_ALIASES


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
    if _is_region_place_label(label):
        return "region"
    if "school" in key or "classroom" in key:
        return "school"
    if _is_room_place_key(key):
        return "room"
    if "volcano" in key or key in {"hualalai", "kilauea"}:
        return "volcano"
    if "garden" in key or "rainforest" in key or "waterfall" in key:
        return "nature"
    if "ocean" in key or "beach" in key or "hot springs" in key or "lake" in key:
        return "water"
    if "park" in key or "playground" in key:
        return "park"
    if "restaurant" in key:
        return "restaurant"
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
    relatedness = _event_relatedness(event)
    if relatedness in {"likely_unrelated", "unrelated"}:
        return "unrelated_content"
    if relatedness == "non_content":
        return "non_content"
    event_type = _metadata_value(event, "event_type")
    if event_type in {"school", "home", "medical", "travel"}:
        return str(event_type)
    title = _normalize_key(event.get("title"))
    if "zoo" in title or "safari" in title:
        return "animals"
    return "unreviewed"


def _is_contextual_event(event: dict[str, Any]) -> bool:
    return _event_relatedness(event) not in {"likely_unrelated", "unrelated", "non_content"}


def _event_relatedness(event: dict[str, Any]) -> str:
    return str(_metadata_value(event, "relatedness") or "").lower()


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
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
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

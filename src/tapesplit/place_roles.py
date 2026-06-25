from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


FILMING_LOCATION_ROLES = {
    "administrative_context",
    "explicit_location_anchor",
    "generic_scene_type",
    "visible_place",
}

NON_FILMING_LOCATION_ROLES = {
    "ambiguous_place_reference",
    "mentioned_destination",
    "not_a_location",
    "travel_plan",
}

GENERIC_PLACE_KEYS = {
    "apartment",
    "apartment complex",
    "beach",
    "botanical garden",
    "chapel",
    "classroom",
    "community garden",
    "community gardens",
    "garden",
    "gym",
    "hall",
    "home",
    "house",
    "indoor hall",
    "indoor hall gym",
    "kindergarten",
    "kitchen",
    "lake",
    "library",
    "living room",
    "ocean",
    "park",
    "playground",
    "restaurant",
    "room",
    "school",
    "school hall",
    "street",
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

AMBIGUITY_PATTERNS = [
    "appears to be",
    "could be",
    "looks like",
    "may be",
    "maybe",
    "possibly",
    "probably",
    "seems to be",
    "unclear",
]

TRAVEL_PLAN_PATTERNS = [
    "buys tickets to",
    "buy tickets to",
    "bought tickets to",
    "flight to",
    "fly to",
    "going to",
    "plans to go to",
    "tickets to",
]

DIRECT_LOCATION_PATTERNS = [
    "at",
    "in",
    "inside",
    "near",
    "outside",
    "we are at",
    "we are in",
]

CYRILLIC_PLACE_STEMS = {
    "alaska": ["аляск"],
    "anchorage": ["анкоридж"],
    "moscow": ["москв"],
    "russia": ["росси", "русск"],
}


def build_place_roles_for_project(project_dir: Path, *, prefer_canonical: bool = True) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    events = read_jsonl(project / "canonical_events.jsonl") if prefer_canonical else []
    if not events:
        events = read_jsonl(project / "events.jsonl") + read_jsonl(project / "gemini_events.jsonl")
    evidence = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence if row.get("id")}
    rows = infer_event_place_roles(events, evidence_by_id)
    output = project / "event_place_roles.jsonl"
    _write_jsonl(output, rows)
    return {
        "project": str(project),
        "event_place_roles": len(rows),
        "by_role": dict(Counter(row["role"] for row in rows)),
        "included_in_place_groups": sum(1 for row in rows if row.get("include_in_place_groups")),
        "outputs": {"event_place_roles": str(output)},
    }


def infer_event_place_roles(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda row: _number_or_large(row.get("start_s"))):
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        for place in _metadata_list(event, "place_candidates"):
            direct_evidence = _direct_place_evidence(place, event, evidence_by_id)
            role_info = _classify_place_role(place, event, direct_evidence, evidence_by_id)
            rows.append(
                {
                    "id": f"event_place_role_{len(rows) + 1:06d}",
                    "canonical_event_id": event_id,
                    "source_video_ids": _event_source_video_ids(event),
                    "label": place,
                    "normalized_label": _normalize_key(place),
                    "role": role_info["role"],
                    "role_family": "filming_location_candidate"
                    if role_info["role"] in FILMING_LOCATION_ROLES
                    else "non_filming_context",
                    "include_in_place_groups": role_info["role"] in FILMING_LOCATION_ROLES,
                    "exportable_as_gps_candidate": bool(role_info["exportable_as_gps_candidate"]),
                    "confidence": role_info["confidence"],
                    "source_label": role_info["source_label"],
                    "evidence_ids": _unique_items(
                        [
                            *[str(item) for item in event.get("evidence_ids") or [] if item],
                            *[str(row.get("id")) for row in direct_evidence if row.get("id")],
                        ]
                    ),
                    "direct_evidence_ids": [str(row.get("id")) for row in direct_evidence if row.get("id")],
                    "evidence_texts": role_info["evidence_texts"],
                    "basis": role_info["basis"],
                    "review_status": role_info["review_status"],
                    "notes": role_info["notes"],
                    "candidate_options": _role_candidate_options(place, role_info),
                }
            )
    return rows


def events_with_role_filtered_places(
    events: list[dict[str, Any]],
    place_roles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    roles_by_event_label: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for role in place_roles:
        key = (str(role.get("canonical_event_id") or ""), str(role.get("normalized_label") or ""))
        roles_by_event_label.setdefault(key, []).append(role)

    filtered_events = []
    for event in events:
        event_id = str(event.get("id") or "")
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        raw_places = _metadata_list(event, "place_candidates")
        included_places = []
        role_summaries = []
        for place in raw_places:
            roles = roles_by_event_label.get((event_id, _normalize_key(place)), [])
            include = any(role.get("include_in_place_groups") for role in roles)
            if include and place not in included_places:
                included_places.append(place)
            for role in roles:
                role_summaries.append(
                    {
                        "event_place_role_id": role.get("id"),
                        "label": role.get("label"),
                        "normalized_label": role.get("normalized_label"),
                        "role": role.get("role"),
                        "source_label": role.get("source_label"),
                        "confidence": role.get("confidence"),
                        "include_in_place_groups": role.get("include_in_place_groups"),
                        "basis": role.get("basis") or [],
                        "evidence_texts": role.get("evidence_texts") or [],
                    }
                )

        next_metadata = {
            **metadata,
            "raw_place_candidates": raw_places,
            "place_candidates": included_places,
            "place_roles": role_summaries,
        }
        filtered_events.append({**event, "metadata": next_metadata})
    return filtered_events


def _classify_place_role(
    place: str,
    event: dict[str, Any],
    direct_evidence: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    key = _normalize_key(place)
    all_texts = _all_place_context_texts(event, direct_evidence, evidence_by_id)
    texts = _place_evidence_texts(place, all_texts)
    combined_text = " ".join(all_texts).casefold()
    basis = []
    notes = []

    if not key:
        return _role_info(
            "not_a_location",
            confidence=0.0,
            source_label="not a location",
            basis=["empty_place_label"],
            notes=["Empty place label."],
            evidence_texts=texts,
        )

    if _is_travel_plan_mention(place, combined_text, texts):
        return _role_info(
            "travel_plan",
            confidence=0.82,
            source_label="mentioned travel plan",
            basis=["travel_plan_language"],
            notes=["Mentioned as a destination or plan, not where the camera appears to be recording."],
            evidence_texts=texts,
            review_status="unreviewed",
        )

    if _is_event_level_ambiguous_location(place, event, combined_text):
        return _role_info(
            "ambiguous_place_reference",
            confidence=0.58,
            source_label="ambiguous model/narration mention",
            basis=["event_level_location_uncertainty"],
            notes=["The event-level wording is uncertain about where filming occurred, so this should not drive album titles or GPS metadata."],
            evidence_texts=texts,
            review_status="needs_review",
        )

    if _is_ambiguous_place_mention(place, combined_text):
        return _role_info(
            "ambiguous_place_reference",
            confidence=0.62,
            source_label="ambiguous model/narration mention",
            basis=["ambiguous_language"],
            notes=["Place is mentioned with uncertainty, so it should not drive album titles or GPS metadata."],
            evidence_texts=texts,
            review_status="needs_review",
        )

    if key in GENERIC_PLACE_KEYS:
        return _role_info(
            "generic_scene_type",
            confidence=0.72,
            source_label="visual scene type",
            basis=["generic_place_scene_type"],
            notes=["Useful scene context, not an exact address or GPS location."],
            evidence_texts=texts,
            review_status="needs_review",
        )

    if _is_region_label(place):
        basis.append("administrative_place_context")
        return _role_info(
            "administrative_context",
            confidence=0.78,
            source_label="administrative context",
            basis=basis,
            notes=["Broader region context; not exact GPS metadata by itself."],
            evidence_texts=texts,
        )

    if _has_direct_location_language(place, combined_text):
        return _role_info(
            "explicit_location_anchor",
            confidence=0.84,
            source_label="direct location mention",
            basis=["direct_location_language"],
            notes=["Appears to be directly stated or strongly anchored by narration/OCR/model evidence."],
            evidence_texts=texts,
            exportable_as_gps_candidate=False,
        )

    return _role_info(
        "visible_place",
        confidence=0.74,
        source_label="visual/model place clue",
        basis=["model_place_candidate"],
        notes=["Appears to be a filming-location clue, but export still requires confirmation or geocoding."],
        evidence_texts=texts,
    )


def _role_info(
    role: str,
    *,
    confidence: float,
    source_label: str,
    basis: list[str],
    notes: list[str],
    evidence_texts: list[str],
    review_status: str | None = None,
    exportable_as_gps_candidate: bool = False,
) -> dict[str, Any]:
    return {
        "role": role,
        "confidence": round(confidence, 3),
        "source_label": source_label,
        "basis": basis,
        "notes": notes,
        "evidence_texts": evidence_texts[:6],
        "review_status": review_status or ("needs_review" if role in NON_FILMING_LOCATION_ROLES else "unreviewed"),
        "exportable_as_gps_candidate": exportable_as_gps_candidate,
    }


def _role_candidate_options(place: str, selected: dict[str, Any]) -> list[dict[str, Any]]:
    options = [
        {
            "id": "selected",
            "label": place,
            "role": selected["role"],
            "source_label": selected["source_label"],
            "confidence": selected["confidence"],
            "selected": True,
            "basis": selected["basis"],
        }
    ]
    if selected["role"] in FILMING_LOCATION_ROLES:
        options.append(
            {
                "id": "mentioned_only",
                "label": place,
                "role": "ambiguous_place_reference",
                "source_label": "mentioned only",
                "confidence": 0.4,
                "selected": False,
                "basis": ["human_override_option"],
            }
        )
    else:
        options.append(
            {
                "id": "filming_location",
                "label": place,
                "role": "visible_place",
                "source_label": "filming location",
                "confidence": 0.4,
                "selected": False,
                "basis": ["human_override_option"],
            }
        )
    return options


def _all_place_context_texts(
    event: dict[str, Any],
    direct_evidence: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    texts = []
    for key in ["title", "summary"]:
        value = str(event.get(key) or "").strip()
        if value:
            texts.append(value)
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    for item in metadata.get("evidence_text") or []:
        if str(item).strip():
            texts.append(str(item).strip())
    for evidence_id in event.get("evidence_ids") or []:
        evidence = evidence_by_id.get(str(evidence_id)) or {}
        text = str(evidence.get("text") or "").strip()
        if text:
            texts.append(text)
    for evidence in direct_evidence:
        text = str(evidence.get("text") or evidence.get("metadata", {}).get("evidence_text") or "").strip()
        if text:
            texts.append(text)
    return _unique_items(texts)


def _place_evidence_texts(place: str, texts: list[str]) -> list[str]:
    aliases = _place_aliases(place)
    matched = [text for text in _unique_items(texts) if _text_mentions_any_alias(text, aliases)]
    return matched or _unique_items(texts)[:4]


def _direct_place_evidence(
    place: str,
    event: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    aliases = {_normalize_key(alias) for alias in _place_aliases(place)}
    ranges = _source_ranges(event)
    rows = []
    for evidence in evidence_by_id.values():
        if evidence.get("kind") != "gemini_place_candidate":
            continue
        metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
        evidence_name = _normalize_key(metadata.get("name") or evidence.get("value") or evidence.get("text"))
        if evidence_name not in aliases:
            continue
        if not _evidence_overlaps_event_source_range(evidence, ranges):
            continue
        rows.append(evidence)
    return sorted(rows, key=lambda row: _number_or_large(row.get("start_s")))


def _source_ranges(event: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    ranges = metadata.get("source_ranges") if isinstance(metadata.get("source_ranges"), list) else []
    if ranges:
        return [row for row in ranges if isinstance(row, dict)]
    return [
        {
            "source_video_id": event.get("source_video_id"),
            "start_s": event.get("start_s"),
            "end_s": event.get("end_s"),
            "timeline_start_s": event.get("start_s"),
            "timeline_end_s": event.get("end_s"),
        }
    ]


def _evidence_overlaps_event_source_range(evidence: dict[str, Any], ranges: list[dict[str, Any]]) -> bool:
    source_video_id = str(evidence.get("source_video_id") or "")
    evidence_start = _number_or_none(evidence.get("start_s"))
    evidence_end = _number_or_none(evidence.get("end_s")) or evidence_start
    if evidence_start is None or evidence_end is None:
        return False
    for source_range in ranges:
        if source_video_id and str(source_range.get("source_video_id") or "") != source_video_id:
            continue
        for start_key, end_key in [("start_s", "end_s"), ("timeline_start_s", "timeline_end_s")]:
            start = _number_or_none(source_range.get(start_key))
            end = _number_or_none(source_range.get(end_key)) or start
            if start is None or end is None:
                continue
            if evidence_start <= end and start <= evidence_end:
                return True
    return False


def _is_travel_plan_mention(place: str, combined_text: str, texts: list[str]) -> bool:
    aliases = _place_aliases(place)
    for alias in aliases:
        alias_key = alias.casefold()
        for pattern in TRAVEL_PLAN_PATTERNS:
            if pattern in combined_text and alias_key in combined_text:
                if _phrase_before_alias(combined_text, pattern, alias_key, max_chars=90):
                    return True
        if re.search(rf"\b(to|toward|towards)\s+{re.escape(alias_key)}\b", combined_text):
            if any(pattern in combined_text for pattern in ["ticket", "flight", "going", "plans", "travel"]):
                return True

    key = _normalize_key(place)
    cyrillic_stems = CYRILLIC_PLACE_STEMS.get(key, [])
    for text in texts:
        lowered = text.casefold()
        for stem in cyrillic_stems:
            if stem in lowered and re.search(rf"\b(на|в)\s+\w*{re.escape(stem)}", lowered):
                if any(token in lowered for token in ["билет", "лет", "ехать", "поезд", "собира", "также"]):
                    return True
                if re.search(rf"\bна\s+\w*{re.escape(stem)}", lowered):
                    return True
    return False


def _is_ambiguous_place_mention(place: str, combined_text: str) -> bool:
    aliases = [alias.casefold() for alias in _place_aliases(place)]
    for pattern in AMBIGUITY_PATTERNS:
        if pattern not in combined_text:
            continue
        for alias in aliases:
            if alias in combined_text and _near_phrase(combined_text, pattern, alias, max_chars=120):
                return True
    return False


def _is_event_level_ambiguous_location(place: str, event: dict[str, Any], combined_text: str) -> bool:
    key = _normalize_key(place)
    if key in GENERIC_PLACE_KEYS:
        return False
    if not any(pattern in combined_text for pattern in AMBIGUITY_PATTERNS):
        return False
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    event_type = str(metadata.get("event_type") or event.get("event_type") or "")
    location_topic = any(token in combined_text for token in ["location", "place", "tour", "trip", "travel"])
    if event_type == "travel" and location_topic:
        return True
    return False


def _has_direct_location_language(place: str, combined_text: str) -> bool:
    aliases = [alias.casefold() for alias in _place_aliases(place)]
    for alias in aliases:
        if re.search(rf"\b(at|in|inside|near|outside)\s+(?:the\s+)?{re.escape(alias)}\b", combined_text):
            return True
        for pattern in DIRECT_LOCATION_PATTERNS:
            if pattern in combined_text and alias in combined_text and _near_phrase(combined_text, pattern, alias, max_chars=50):
                return True
    key = _normalize_key(place)
    for stem in CYRILLIC_PLACE_STEMS.get(key, []):
        if re.search(rf"\b(в|на|у)\s+\w*{re.escape(stem)}", combined_text):
            return True
    return False


def _near_phrase(text: str, left: str, right: str, *, max_chars: int) -> bool:
    left_positions = [match.start() for match in re.finditer(re.escape(left), text)]
    right_positions = [match.start() for match in re.finditer(re.escape(right), text)]
    return any(abs(left_pos - right_pos) <= max_chars for left_pos in left_positions for right_pos in right_positions)


def _phrase_before_alias(text: str, phrase: str, alias: str, *, max_chars: int) -> bool:
    phrase_positions = [match.start() for match in re.finditer(re.escape(phrase), text)]
    alias_positions = [match.start() for match in re.finditer(re.escape(alias), text)]
    return any(0 <= alias_pos - phrase_pos <= max_chars for phrase_pos in phrase_positions for alias_pos in alias_positions)


def _place_aliases(place: str) -> list[str]:
    key = _normalize_key(place)
    aliases = [str(place).casefold(), key]
    aliases.extend(CYRILLIC_PLACE_STEMS.get(key, []))
    if "," in str(place):
        aliases.append(str(place).split(",", 1)[0].casefold().strip())
    return _unique_items(alias for alias in aliases if alias)


def _text_mentions_any_alias(text: str, aliases: list[str]) -> bool:
    lowered = text.casefold()
    return any(alias.casefold() in lowered for alias in aliases)


def _is_region_label(value: Any) -> bool:
    text = str(value or "")
    key = _normalize_key(value)
    return "," in text or key in US_STATE_KEYS or key in COUNTRY_KEYS


def _metadata_list(event: dict[str, Any], key: str) -> list[str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    value = metadata.get(key) or event.get(key)
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _event_source_video_ids(event: dict[str, Any]) -> list[str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_ids = event.get("source_video_ids") or metadata.get("source_video_ids")
    if isinstance(source_ids, list):
        return [str(item) for item in source_ids if item]
    return [str(event.get("source_video_id"))] if event.get("source_video_id") else []


def _normalize_key(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


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

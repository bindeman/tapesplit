from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


KINSHIP_PATTERNS = [
    {
        "predicate": "mother_candidate",
        "subject_label": "Unresolved mother",
        "terms": [
            "mom",
            "mommy",
            "mama",
            "mother",
            "мама",
            "маме",
            "маму",
            "мамой",
            "мамочка",
            "мамочке",
            "мать",
        ],
    },
    {
        "predicate": "father_candidate",
        "subject_label": "Unresolved father",
        "terms": [
            "dad",
            "daddy",
            "papa",
            "father",
            "папа",
            "папе",
            "папу",
            "папой",
            "папочка",
            "отец",
        ],
    },
    {
        "predicate": "grandparent_candidate",
        "subject_label": "Unresolved grandmother",
        "terms": [
            "grandma",
            "grandmother",
            "granny",
            "nana",
            "babushka",
            "бабушка",
            "бабушке",
            "бабушку",
            "бабушкой",
        ],
    },
    {
        "predicate": "grandparent_candidate",
        "subject_label": "Unresolved grandfather",
        "terms": [
            "grandpa",
            "grandfather",
            "granddad",
            "dedushka",
            "дедушка",
            "дедушке",
            "дедушку",
            "дедушкой",
        ],
    },
    {
        "predicate": "sibling_candidate",
        "subject_label": "Unresolved brother",
        "terms": ["brother", "брат", "брата", "брату", "братом"],
    },
    {
        "predicate": "sibling_candidate",
        "subject_label": "Unresolved sister",
        "terms": ["sister", "сестра", "сестру", "сестре", "сестрой"],
    },
]

CYRILLIC_NAME_VARIANTS = {
    "filip": {"филипп", "филиппа", "филиппу", "филиппом", "филечка", "филечку", "филей"},
    "philip": {"филипп", "филиппа", "филиппу", "филиппом", "филечка", "филечку", "филей"},
    "phillip": {"филипп", "филиппа", "филиппу", "филиппом", "филечка", "филечку", "филей"},
    "elena": {"елена", "елену", "елене", "лена", "лену", "лене"},
    "ilya": {"илья", "илью", "илье", "ильей"},
    "vera": {"вера", "веру", "вере", "верой"},
}

ROLE_ONLY_PEOPLE = {
    "adult",
    "adults",
    "boy",
    "boys",
    "child",
    "children",
    "class",
    "family",
    "friend",
    "friends",
    "girl",
    "girls",
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


def build_relationship_candidates(project_dir: Path, *, context_seconds: float = 8.0) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    visibility = build_visibility_filter(project)
    all_evidence = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in all_evidence if row.get("id")}
    events = sorted(
        [
            row
            for row in read_jsonl(project / "canonical_events.jsonl")
            if visibility.visible_row(row, evidence_by_id=evidence_by_id)
        ],
        key=lambda row: _number_or_large(row.get("start_s")),
    )
    evidence = [row for row in all_evidence if visibility.visible_row(row)]
    transcripts = _relationship_observations(project, evidence, visibility=visibility)
    person_entities = _load_person_entities(project, events)

    relationship_buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for segment in transcripts:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        matches = _kinship_matches(text)
        if not matches:
            continue
        event = _event_for_segment(events, segment)
        context_text = _context_text(transcripts, segment, context_seconds=context_seconds)
        object_entities = _object_entities_for_context(
            text=context_text,
            event=event,
            person_entities=person_entities,
        )
        if not object_entities:
            continue
        evidence_id = segment.get("evidence_id") or segment.get("id")
        for match in matches:
            for object_entity in object_entities:
                key = (
                    match["predicate"],
                    match["subject_label"],
                    object_entity["id"],
                    str(event.get("id") if event else ""),
                )
                bucket = relationship_buckets.setdefault(
                    key,
                    {
                        "predicate": match["predicate"],
                        "subject_label": match["subject_label"],
                        "object_entity_id": object_entity["id"],
                        "object_label": object_entity["label"],
                        "event": event,
                        "source_video_ids": set(),
                        "evidence_ids": [],
                        "transcript_segment_ids": [],
                        "terms": set(),
                        "source_texts": [],
                        "start_s": None,
                        "end_s": None,
                        "context_source": object_entity["context_source"],
                    },
                )
                if segment.get("source_video_id"):
                    bucket["source_video_ids"].add(segment.get("source_video_id"))
                if evidence_id and evidence_id not in bucket["evidence_ids"]:
                    bucket["evidence_ids"].append(evidence_id)
                if segment.get("id") and segment.get("id") not in bucket["transcript_segment_ids"]:
                    bucket["transcript_segment_ids"].append(segment.get("id"))
                bucket["terms"].update(match["terms"])
                if text not in bucket["source_texts"]:
                    bucket["source_texts"].append(text)
                bucket["start_s"] = _min_number(bucket["start_s"], segment.get("start_s"))
                bucket["end_s"] = _max_number(bucket["end_s"], segment.get("end_s"))

    candidates = [_candidate_from_bucket(index, bucket) for index, bucket in enumerate(relationship_buckets.values(), start=1)]
    review_tasks = [_review_task_from_candidate(index, candidate) for index, candidate in enumerate(candidates, start=1)]

    relationship_output = project / "relationship_candidates.jsonl"
    review_output = project / "relationship_review_tasks.jsonl"
    _write_jsonl(relationship_output, candidates)
    _write_jsonl(review_output, review_tasks)

    return {
        "project": str(project),
        "relationship_candidates": len(candidates),
        "relationship_review_tasks": len(review_tasks),
        "outputs": {
            "relationship_candidates": str(relationship_output),
            "relationship_review_tasks": str(review_output),
        },
        "by_predicate": _count_by(candidates, "predicate"),
    }


def _candidate_from_bucket(index: int, bucket: dict[str, Any]) -> dict[str, Any]:
    event = bucket.get("event") if isinstance(bucket.get("event"), dict) else None
    context_source = bucket.get("context_source")
    base_confidence = 0.72 if context_source == "name_mention" else 0.58
    confidence = min(0.9, base_confidence + min(len(bucket["evidence_ids"]) - 1, 3) * 0.04)
    event_ids = [event["id"]] if event and event.get("id") else []
    signals = [f"kinship term '{term}' in transcript" for term in sorted(bucket["terms"])]
    if context_source == "name_mention":
        signals.append(f"name or alias for {bucket['object_label']} appears nearby")
    elif context_source == "event_subject":
        signals.append(f"event context points to {bucket['object_label']}")
    return {
        "id": f"relationship_candidate_{index:06d}",
        "subject_entity_id": _role_entity_id(bucket["subject_label"], bucket["object_entity_id"]),
        "subject_label": bucket["subject_label"],
        "predicate": bucket["predicate"],
        "object_entity_id": bucket["object_entity_id"],
        "object_label": bucket["object_label"],
        "direction": "subject_to_object",
        "scope": {
            "canonical_event_ids": event_ids,
            "source_video_ids": sorted(bucket["source_video_ids"]),
            "start_s": bucket["start_s"],
            "end_s": bucket["end_s"],
        },
        "confidence": round(confidence, 2),
        "supporting_signals": signals,
        "evidence_ids": bucket["evidence_ids"],
        "contradicting_evidence_ids": [],
        "source": "local_relationship_resolver",
        "review_status": "needs_review",
        "metadata": {
            "context_source": context_source,
            "terms": sorted(bucket["terms"]),
            "transcript_segment_ids": bucket["transcript_segment_ids"],
            "source_texts": bucket["source_texts"][:5],
        },
    }


def _review_task_from_candidate(index: int, candidate: dict[str, Any]) -> dict[str, Any]:
    predicate_label = _predicate_label(str(candidate.get("predicate") or "relationship"))
    subject = str(candidate.get("subject_label") or "this person").replace("Unresolved ", "").lower()
    obj = str(candidate.get("object_label") or "the named person")
    return {
        "id": f"relationship_review_task_{index:06d}",
        "task_type": "confirm_relationship",
        "question": f"Is the person referred to as {subject} likely {obj}'s {predicate_label}?",
        "candidate_ids": [candidate.get("id")],
        "evidence_ids": candidate.get("evidence_ids", []),
        "priority": "high" if float(candidate.get("confidence") or 0.0) >= 0.7 else "medium",
        "review_status": "open",
        "thumbnail_paths": [],
    }


def _load_person_entities(project: Path, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities = []
    seen: set[str] = set()
    for group in read_jsonl(project / "people_groups.jsonl"):
        label = str(group.get("label") or "").strip()
        aliases = [str(alias) for alias in group.get("aliases", []) if str(alias).strip()]
        key = str((group.get("metadata") or {}).get("normalized_key") or _normalize_key(label))
        if not label or key in ROLE_ONLY_PEOPLE:
            continue
        entities.append(
            {
                "id": str(group.get("id") or f"person_candidate_{key}"),
                "key": key,
                "label": label,
                "aliases": _alias_variants([label, *aliases], key),
            }
        )
        seen.add(key)

    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        for person in metadata.get("people") or []:
            label = str(person).strip()
            key = _normalize_key(label)
            if not label or not key or key in seen or key in ROLE_ONLY_PEOPLE:
                continue
            if any(_labels_match(label, entity) for entity in entities):
                continue
            entities.append(
                {
                    "id": f"person_candidate_{key}",
                    "key": key,
                    "label": label,
                    "aliases": _alias_variants([label], key),
                }
            )
            seen.add(key)
    return entities


def _relationship_observations(
    project: Path,
    evidence: list[dict[str, Any]],
    *,
    visibility: Any,
) -> list[dict[str, Any]]:
    evidence_by_transcript_id = _evidence_by_transcript_id(evidence)
    observations = []
    seen: set[tuple[str, float | None, float | None, str]] = set()

    for row in read_jsonl(project / "transcript_segments.jsonl"):
        if visibility.excluded_row(row):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        observation = {
            **row,
            "evidence_id": evidence_by_transcript_id.get(str(row.get("id"))) or row.get("id"),
            "observation_source": "transcript_segments",
        }
        observations.append(observation)
        seen.add(_observation_key(observation))

    for row in evidence:
        if row.get("modality") != "transcript":
            continue
        if row.get("kind") == "local_transcript_segment":
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        observation = {
            "id": f"tr_obs_{row.get('id')}",
            "evidence_id": row.get("id"),
            "source_video_id": row.get("source_video_id"),
            "start_s": row.get("start_s"),
            "end_s": row.get("end_s"),
            "text": text,
            "language": row.get("language"),
            "observation_source": row.get("kind") or "evidence_transcript",
        }
        key = _observation_key(observation)
        if key in seen:
            continue
        seen.add(key)
        observations.append(observation)

    return sorted(observations, key=lambda row: _number_or_large(row.get("start_s")))


def _object_entities_for_context(
    *,
    text: str,
    event: dict[str, Any] | None,
    person_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    direct_matches = []
    for entity in person_entities:
        if _contains_any_term(text, entity["aliases"]):
            direct_matches.append({**entity, "context_source": "name_mention"})
    if direct_matches:
        return _unique_entities(direct_matches)

    if not event:
        return []
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    event_people = [
        entity
        for entity in person_entities
        if any(_labels_match(person, entity) for person in metadata.get("people") or [])
    ]
    if len(event_people) == 1:
        return [{**event_people[0], "context_source": "event_subject"}]
    return []


def _labels_match(label: Any, entity: dict[str, Any]) -> bool:
    normalized = _normalize_key(str(label or ""))
    return normalized == entity.get("key") or normalized in {_normalize_key(alias) for alias in entity.get("aliases", [])}


def _event_for_segment(events: list[dict[str, Any]], segment: dict[str, Any]) -> dict[str, Any] | None:
    source_video_id = segment.get("source_video_id")
    start_s = _number_or_none(segment.get("start_s"))
    if start_s is None:
        return None
    for event in events:
        event_start = _number_or_none(event.get("start_s"))
        event_end = _number_or_none(event.get("end_s"))
        if event_start is None or event_end is None:
            continue
        if event_start - 1.0 <= start_s <= event_end + 1.0:
            if not source_video_id:
                return event
            return event
    return None


def _context_text(transcripts: list[dict[str, Any]], segment: dict[str, Any], *, context_seconds: float) -> str:
    center = _number_or_none(segment.get("start_s"))
    if center is None:
        return str(segment.get("text") or "")
    source_video_id = segment.get("source_video_id")
    parts = []
    for other in transcripts:
        if source_video_id and other.get("source_video_id") != source_video_id:
            continue
        start = _number_or_none(other.get("start_s"))
        if start is None:
            continue
        if abs(start - center) <= context_seconds:
            parts.append(str(other.get("text") or ""))
    return " ".join(parts)


def _kinship_matches(text: str) -> list[dict[str, Any]]:
    matches = []
    for pattern in KINSHIP_PATTERNS:
        terms = [term for term in pattern["terms"] if _contains_term(text, term)]
        if terms:
            matches.append(
                {
                    "predicate": pattern["predicate"],
                    "subject_label": pattern["subject_label"],
                    "terms": terms,
                }
            )
    return matches


def _contains_any_term(text: str, terms: set[str]) -> bool:
    return any(_contains_term(text, term) for term in terms)


def _contains_term(text: str, term: str) -> bool:
    text = text.casefold()
    term = term.casefold()
    if not term:
        return False
    if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, flags=re.IGNORECASE):
        return True
    return False


def _alias_variants(labels: list[str], key: str) -> set[str]:
    variants = {_clean_label(label) for label in labels if _clean_label(label)}
    variants.add(key)
    for label in labels:
        for part in re.split(r"[/,()]+|\s+", label):
            cleaned = _clean_label(part)
            if cleaned and cleaned not in ROLE_ONLY_PEOPLE:
                variants.add(cleaned)
    variants.update(CYRILLIC_NAME_VARIANTS.get(key, set()))
    if key in {"filip", "filipp"}:
        variants.update(CYRILLIC_NAME_VARIANTS["philip"])
    return {variant for variant in variants if variant}


def _evidence_by_transcript_id(evidence: list[dict[str, Any]]) -> dict[str, str]:
    by_id = {}
    for row in evidence:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        transcript_id = metadata.get("transcript_segment_id")
        evidence_id = row.get("id")
        if transcript_id and evidence_id:
            by_id[str(transcript_id)] = str(evidence_id)
    return by_id


def _observation_key(row: dict[str, Any]) -> tuple[str, float | None, float | None, str]:
    return (
        str(row.get("source_video_id") or ""),
        _number_or_none(row.get("start_s")),
        _number_or_none(row.get("end_s")),
        str(row.get("text") or "").strip().casefold(),
    )


def _role_entity_id(subject_label: str, object_entity_id: str) -> str:
    key = _normalize_key(subject_label.replace("Unresolved", "").strip())
    object_key = re.sub(r"[^a-zA-Z0-9_]+", "_", object_entity_id).strip("_").lower()
    return f"role_entity_{key}_of_{object_key}"


def _predicate_label(predicate: str) -> str:
    labels = {
        "mother_candidate": "mother",
        "father_candidate": "father",
        "grandparent_candidate": "grandparent",
        "sibling_candidate": "sibling",
    }
    return labels.get(predicate, predicate.replace("_candidate", "").replace("_", " "))


def _unique_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for entity in entities:
        if entity["id"] in seen:
            continue
        seen.add(entity["id"])
        unique.append(entity)
    return unique


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)
    if not rows:
        path.write_text("", encoding="utf-8")


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _clean_label(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _normalize_key(value: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", " ", value.casefold())
    return " ".join(cleaned.split())


def _number_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_or_large(value: Any) -> float:
    number = _number_or_none(value)
    return number if number is not None else 1_000_000_000.0


def _min_number(left: Any, right: Any) -> float | None:
    left_number = _number_or_none(left)
    right_number = _number_or_none(right)
    if left_number is None:
        return right_number
    if right_number is None:
        return left_number
    return min(left_number, right_number)


def _max_number(left: Any, right: Any) -> float | None:
    left_number = _number_or_none(left)
    right_number = _number_or_none(right)
    if left_number is None:
        return right_number
    if right_number is None:
        return left_number
    return max(left_number, right_number)

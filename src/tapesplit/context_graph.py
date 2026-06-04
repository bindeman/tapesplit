from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


def build_context_graph(project_dir: Path) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    events = read_jsonl(project / "canonical_events.jsonl")
    people_groups = read_jsonl(project / "people_groups.jsonl")
    place_groups = read_jsonl(project / "place_groups.jsonl")
    date_groups = read_jsonl(project / "date_groups.jsonl")
    event_groups = read_jsonl(project / "event_groups.jsonl")
    relationship_candidates = read_jsonl(project / "relationship_candidates.jsonl")
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}
    events = [_event_with_evidence_sources(event, evidence_by_id) for event in events]

    events_by_id = {str(event.get("id")): event for event in events if event.get("id")}
    people_by_event = _rows_by_event(people_groups)
    places_by_event = _rows_by_event(place_groups)
    dates_by_event = _rows_by_event(date_groups)

    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        event_people = people_by_event.get(event_id, [])
        event_places = places_by_event.get(event_id, [])
        event_dates = dates_by_event.get(event_id, [])

        for person in event_people:
            _add_edge(
                buckets,
                subject=_node("person", person),
                predicate="appears_in_event",
                object_=_event_node(event),
                event=event,
                evidence_ids=_event_evidence_ids(event),
                confidence=_combine_confidence(person.get("confidence"), event.get("confidence")),
                source="context_graph_builder",
                supporting_signal=f"{_label(person)} appears in {_label(event)}",
            )

        for left, right in combinations(event_people, 2):
            _add_edge(
                buckets,
                subject=_node("person", left),
                predicate="appears_with",
                object_=_node("person", right),
                event=event,
                evidence_ids=_event_evidence_ids(event),
                confidence=_combine_confidence(left.get("confidence"), right.get("confidence"), event.get("confidence")),
                source="context_graph_builder",
                supporting_signal=f"{_label(left)} and {_label(right)} appear in the same event",
                undirected=True,
            )

        for place in event_places:
            _add_edge(
                buckets,
                subject=_event_node(event),
                predicate="event_at_place",
                object_=_node("place", place),
                event=event,
                evidence_ids=_event_evidence_ids(event),
                confidence=_combine_confidence(place.get("confidence"), event.get("confidence")),
                source="context_graph_builder",
                supporting_signal=f"{_label(event)} is associated with {_label(place)}",
            )
            for person in event_people:
                _add_edge(
                    buckets,
                    subject=_node("person", person),
                    predicate="person_place_context",
                    object_=_node("place", place),
                    event=event,
                    evidence_ids=_event_evidence_ids(event),
                    confidence=_combine_confidence(person.get("confidence"), place.get("confidence"), event.get("confidence")),
                    source="context_graph_builder",
                    supporting_signal=f"{_label(person)} appears in an event associated with {_label(place)}",
                )

        for date in event_dates:
            if date.get("excluded_as_event_date"):
                continue
            _add_edge(
                buckets,
                subject=_event_node(event),
                predicate="event_date_context",
                object_=_node("date", date),
                event=event,
                evidence_ids=_event_evidence_ids(event),
                confidence=_combine_confidence(date.get("confidence"), event.get("confidence")),
                source="context_graph_builder",
                supporting_signal=f"{_label(event)} is associated with date {_label(date)}",
            )

    for event_group in event_groups:
        event_group_id = str(event_group.get("id") or "")
        if not event_group_id:
            continue
        for event_id in event_group.get("canonical_event_ids") or []:
            event = events_by_id.get(str(event_id))
            if not event:
                continue
            _add_edge(
                buckets,
                subject=_event_node(event),
                predicate=_event_group_predicate(event_group),
                object_=_node("event_group", event_group),
                event=event,
                evidence_ids=_event_evidence_ids(event),
                confidence=_combine_confidence(event_group.get("confidence"), event.get("confidence")),
                source="context_graph_builder",
                supporting_signal=f"{_label(event)} belongs to {_label(event_group)}",
            )

    for relationship in relationship_candidates:
        _add_relationship_edge(buckets, relationship, events_by_id)

    edges = [_edge_from_bucket(index, bucket) for index, bucket in enumerate(buckets.values(), start=1)]
    metrics = [_metrics_for_edge(index, edge) for index, edge in enumerate(edges, start=1)]

    context_edges_output = project / "context_edges.jsonl"
    edge_metrics_output = project / "edge_metrics.jsonl"
    _write_jsonl(context_edges_output, edges)
    _write_jsonl(edge_metrics_output, metrics)

    return {
        "project": str(project),
        "context_edges": len(edges),
        "edge_metrics": len(metrics),
        "by_predicate": _count_by(edges, "predicate"),
        "outputs": {
            "context_edges": str(context_edges_output),
            "edge_metrics": str(edge_metrics_output),
        },
    }


def _add_relationship_edge(
    buckets: dict[tuple[str, str, str], dict[str, Any]],
    relationship: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
) -> None:
    relationship_id = str(relationship.get("id") or "")
    subject_id = str(relationship.get("subject_entity_id") or relationship.get("subject_label") or "")
    object_id = str(relationship.get("object_entity_id") or relationship.get("object_label") or "")
    if not relationship_id or not subject_id or not object_id:
        return
    scope = relationship.get("scope") if isinstance(relationship.get("scope"), dict) else {}
    event_ids = [str(item) for item in scope.get("canonical_event_ids") or []]
    event = events_by_id.get(event_ids[0]) if event_ids else None
    _add_edge(
        buckets,
        subject={
            "node_id": subject_id,
            "node_type": "relationship_role",
            "label": str(relationship.get("subject_label") or subject_id),
        },
        predicate="relationship_candidate_edge",
        object_={
            "node_id": object_id,
            "node_type": "person",
            "label": str(relationship.get("object_label") or object_id),
        },
        event=event,
        evidence_ids=[str(item) for item in relationship.get("evidence_ids") or []],
        confidence=_number_or_none(relationship.get("confidence")),
        source="relationship_candidates",
        supporting_signal=str(relationship.get("predicate") or "relationship_candidate"),
        metadata={"relationship_candidate_id": relationship_id},
    )


def _add_edge(
    buckets: dict[tuple[str, str, str], dict[str, Any]],
    *,
    subject: dict[str, Any],
    predicate: str,
    object_: dict[str, Any],
    event: dict[str, Any] | None,
    evidence_ids: list[str],
    confidence: float | None,
    source: str,
    supporting_signal: str,
    undirected: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    subject_id = str(subject["node_id"])
    object_id = str(object_["node_id"])
    if undirected and subject_id > object_id:
        subject, object_ = object_, subject
        subject_id, object_id = object_id, subject_id
    key = (subject_id, predicate, object_id)
    bucket = buckets.setdefault(
        key,
        {
            "subject": subject,
            "predicate": predicate,
            "object": object_,
            "events": {},
            "evidence_ids": [],
            "source_video_ids": set(),
            "supporting_signals": [],
            "confidence_values": [],
            "sources": set(),
            "metadata": {},
        },
    )
    if event and event.get("id"):
        bucket["events"][str(event["id"])] = event
        for source_video_id in _event_source_video_ids(event):
            bucket["source_video_ids"].add(source_video_id)
    for evidence_id in evidence_ids:
        if evidence_id and evidence_id not in bucket["evidence_ids"]:
            bucket["evidence_ids"].append(evidence_id)
    if supporting_signal and supporting_signal not in bucket["supporting_signals"]:
        bucket["supporting_signals"].append(supporting_signal)
    if confidence is not None:
        bucket["confidence_values"].append(confidence)
    bucket["sources"].add(source)
    if metadata:
        bucket["metadata"].update(metadata)


def _edge_from_bucket(index: int, bucket: dict[str, Any]) -> dict[str, Any]:
    events = sorted(bucket["events"].values(), key=lambda event: _number_or_large(event.get("start_s")))
    confidence_values = [value for value in bucket["confidence_values"] if isinstance(value, (int, float))]
    confidence = round(sum(confidence_values) / len(confidence_values), 3) if confidence_values else 0.5
    review_status = "needs_review"
    if confidence >= 0.85 and bucket["predicate"] in {"appears_in_event", "event_at_place", "event_date_context"}:
        review_status = "unreviewed"
    return {
        "id": f"context_edge_{index:06d}",
        "subject_entity_id": bucket["subject"]["node_id"],
        "subject_type": bucket["subject"]["node_type"],
        "subject_label": bucket["subject"]["label"],
        "predicate": bucket["predicate"],
        "object_entity_id": bucket["object"]["node_id"],
        "object_type": bucket["object"]["node_type"],
        "object_label": bucket["object"]["label"],
        "scope": {
            "canonical_event_ids": [str(event.get("id")) for event in events if event.get("id")],
            "source_video_ids": sorted(bucket["source_video_ids"]),
            "start_s": _first_start(events),
            "end_s": _last_end(events),
        },
        "confidence": confidence,
        "evidence_ids": bucket["evidence_ids"],
        "supporting_signals": bucket["supporting_signals"][:10],
        "source": "+".join(sorted(bucket["sources"])),
        "review_status": review_status,
        "metadata": bucket["metadata"],
    }


def _metrics_for_edge(index: int, edge: dict[str, Any]) -> dict[str, Any]:
    scope = edge.get("scope") if isinstance(edge.get("scope"), dict) else {}
    event_ids = [str(item) for item in scope.get("canonical_event_ids") or []]
    source_video_ids = [str(item) for item in scope.get("source_video_ids") or []]
    duration = _duration(scope.get("start_s"), scope.get("end_s"))
    evidence_count = len(edge.get("evidence_ids") or [])
    shared_event_count = len(event_ids)
    multimodal_evidence_count = 1 if evidence_count else 0
    confidence = float(edge.get("confidence") or 0.0)
    computed_weight = min(
        1.0,
        (shared_event_count * 0.16)
        + min(duration / 1800.0, 0.25)
        + min(evidence_count * 0.04, 0.2)
        + confidence * 0.25,
    )
    return {
        "id": f"edge_metric_{index:06d}",
        "edge_id": edge["id"],
        "metric_set": "default_social_context",
        "shared_event_count": shared_event_count,
        "distinct_day_count": 0,
        "distinct_place_count": 0,
        "source_video_count": len(source_video_ids),
        "total_overlap_seconds": round(duration, 3),
        "direct_interaction_count": 0,
        "addressed_by_name_count": 0,
        "central_event_count": 0,
        "era_recurrence_count": 0,
        "multimodal_evidence_count": multimodal_evidence_count,
        "evidence_count": evidence_count,
        "reviewed_confirmation_count": 0,
        "computed_weight": round(computed_weight, 3),
        "review_status": edge.get("review_status", "unreviewed"),
    }


def _rows_by_event(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for event_id in row.get("canonical_event_ids") or []:
            by_event.setdefault(str(event_id), []).append(row)
    return by_event


def _node(node_type: str, row: dict[str, Any]) -> dict[str, str]:
    return {
        "node_id": str(row.get("id") or row.get("label") or row.get("title") or row.get("date_value") or ""),
        "node_type": node_type,
        "label": _label(row),
    }


def _event_node(event: dict[str, Any]) -> dict[str, str]:
    return {
        "node_id": str(event.get("id") or ""),
        "node_type": "event",
        "label": _label(event),
    }


def _label(row: dict[str, Any]) -> str:
    for key in ["label", "title", "date_value", "language", "predicate", "id"]:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return "unknown"


def _event_group_predicate(event_group: dict[str, Any]) -> str:
    group_type = str(event_group.get("group_type") or "")
    if group_type == "trip_context":
        return "same_trip_context"
    if group_type == "same_day_candidate":
        return "same_date_context"
    return "same_event_group_context"


def _event_evidence_ids(event: dict[str, Any]) -> list[str]:
    return [str(item) for item in event.get("evidence_ids") or [] if item]


def _event_source_video_ids(event: dict[str, Any]) -> list[str]:
    source_ids = event.get("source_video_ids")
    if isinstance(source_ids, list):
        return [str(item) for item in source_ids if item]
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_ids = metadata.get("source_video_ids")
    if isinstance(source_ids, list):
        return [str(item) for item in source_ids if item]
    return [str(event.get("source_video_id"))] if event.get("source_video_id") else []


def _event_with_evidence_sources(
    event: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_video_ids = _event_source_video_ids(event)
    for evidence_id in _event_evidence_ids(event):
        source_video_id = evidence_by_id.get(evidence_id, {}).get("source_video_id")
        if source_video_id and str(source_video_id) not in source_video_ids:
            source_video_ids.append(str(source_video_id))
    if not source_video_ids:
        return event
    enriched = dict(event)
    enriched["source_video_ids"] = source_video_ids
    return enriched


def _combine_confidence(*values: Any) -> float | None:
    numbers = [_number_or_none(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def _duration(start: Any, end: Any) -> float:
    start_number = _number_or_none(start)
    end_number = _number_or_none(end)
    if start_number is None or end_number is None:
        return 0.0
    return max(0.0, end_number - start_number)


def _first_start(events: list[dict[str, Any]]) -> float | None:
    starts = [_number_or_none(event.get("start_s")) for event in events]
    starts = [value for value in starts if value is not None]
    return min(starts) if starts else None


def _last_end(events: list[dict[str, Any]]) -> float | None:
    ends = [_number_or_none(event.get("end_s")) for event in events]
    ends = [value for value in ends if value is not None]
    return max(ends) if ends else None


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

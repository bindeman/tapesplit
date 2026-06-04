from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapesplit.storage import read_jsonl, write_json
from tapesplit.visibility import build_visibility_filter


def export_visualization_data(
    project_dir: Path,
    *,
    out_path: Path | None = None,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    output = (out_path or (project / "visualization.json")).expanduser().resolve()
    visibility = build_visibility_filter(project)
    tapes = read_jsonl(project / "tapes.jsonl")
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}

    scenes = _visible_rows(read_jsonl(project / "scenes.jsonl"), visibility, evidence_by_id)
    events = _visible_rows(
        read_jsonl(project / "canonical_events.jsonl")
        or (read_jsonl(project / "events.jsonl") + read_jsonl(project / "gemini_events.jsonl")),
        visibility,
        evidence_by_id,
    )
    albums = _visible_rows(read_jsonl(project / "albums.jsonl"), visibility, evidence_by_id)
    people = _visible_rows(read_jsonl(project / "people_groups.jsonl"), visibility, evidence_by_id)
    places = _visible_rows(read_jsonl(project / "place_groups.jsonl"), visibility, evidence_by_id)
    dates = _visible_rows(read_jsonl(project / "date_groups.jsonl"), visibility, evidence_by_id)
    context_edges = _visible_rows(read_jsonl(project / "context_edges.jsonl"), visibility, evidence_by_id)
    edge_metrics = _visible_rows(read_jsonl(project / "edge_metrics.jsonl"), visibility, evidence_by_id)
    relationships = _visible_rows(read_jsonl(project / "relationship_candidates.jsonl"), visibility, evidence_by_id)
    visual_assets = read_jsonl(project / "visual_assets.jsonl")
    face_observations = read_jsonl(project / "face_observations.jsonl")
    face_clusters = read_jsonl(project / "face_clusters.jsonl")
    face_identity_candidates = read_jsonl(project / "face_identity_candidates.jsonl")

    events_by_id = {str(event.get("id")): event for event in events if event.get("id")}
    people_by_id = {str(person.get("id")): person for person in people if person.get("id")}
    people_by_event = _rows_by_event(people)
    places_by_event = _rows_by_event(places)
    dates_by_event = _rows_by_event(dates)
    places_by_id = {str(place.get("id")): place for place in places if place.get("id")}
    face_clusters_by_id = {str(cluster.get("id")): cluster for cluster in face_clusters if cluster.get("id")}
    assets_by_subject = _assets_by_subject(visual_assets)
    faces_by_person = _faces_by_person(face_observations)
    clusters_by_person_candidate = _clusters_by_person_candidate(face_clusters)
    edge_metrics_by_edge = {str(metric.get("edge_id")): metric for metric in edge_metrics if metric.get("edge_id")}
    place_contexts = _place_context_groups(places, events_by_id)
    place_context_edges = _place_context_edges(context_edges, places_by_id, edge_metrics_by_edge)
    review_queue = _review_queue(
        events=events,
        people=people,
        places=places,
        dates=dates,
        relationship_candidates=relationships,
        place_context_edges=place_context_edges,
        face_identity_candidates=face_identity_candidates,
        events_by_id=events_by_id,
        people_by_id=people_by_id,
        face_clusters_by_id=face_clusters_by_id,
        assets_by_subject=assets_by_subject,
    )

    data = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "project": str(project),
        "media": [_media_record(tape) for tape in tapes],
        "timeline": {
            "events": [
                _event_timeline_entry(
                    event,
                    people_by_event=people_by_event,
                    places_by_event=places_by_event,
                    dates_by_event=dates_by_event,
                    assets_by_subject=assets_by_subject,
                )
                for event in sorted(events, key=lambda row: _number_or_large(row.get("start_s")))
            ],
            "scenes": [
                _scene_timeline_entry(scene, assets_by_subject=assets_by_subject)
                for scene in sorted(scenes, key=lambda row: (str(row.get("source_video_id") or ""), _number_or_large(row.get("start_s"))))
            ],
        },
        "tracks": {
            "people": [
                _people_track(person, events_by_id, faces_by_person)
                | {
                    "candidate_face_clusters": clusters_by_person_candidate.get(str(person.get("id") or ""), []),
                }
                for person in sorted(people, key=lambda row: str(row.get("label") or "").casefold())
            ],
            "places": [
                _place_track(place, events_by_id)
                for place in sorted(places, key=lambda row: _place_sort_key(row))
            ],
            "albums": [
                _album_track(album, events_by_id, assets_by_subject)
                for album in sorted(albums, key=lambda row: _number_or_large(row.get("start_s")))
            ],
        },
        "places": [_place_node(place, events_by_id) for place in sorted(places, key=lambda row: _place_sort_key(row))],
        "place_contexts": place_contexts,
        "people": [
            _person_node(person, faces_by_person)
            | {
                "candidate_face_clusters": clusters_by_person_candidate.get(str(person.get("id") or ""), []),
            }
            for person in sorted(people, key=lambda row: str(row.get("label") or "").casefold())
        ],
        "relationships": {
            "nodes": _graph_nodes(people, places, context_edges),
            "edges": _graph_edges(context_edges, edge_metrics_by_edge),
            "candidates": [_relationship_candidate(row, events_by_id) for row in relationships],
            "place_context_edges": place_context_edges,
            "face_identity_candidates": face_identity_candidates,
        },
        "review_queue": review_queue,
        "assets": {
            "visual": visual_assets,
            "faces": face_observations,
            "face_clusters": face_clusters,
            "face_identity_candidates": face_identity_candidates,
            "by_subject": {key: value for key, value in sorted(assets_by_subject.items())},
        },
        "summary": {
            "source_videos": len(tapes),
            "events": len(events),
            "scenes": len(scenes),
            "people": len(people),
            "places": len(places),
            "relationships": len(relationships),
            "context_edges": len(context_edges),
            "place_contexts": len(place_contexts),
            "review_items": len(review_queue),
            "visual_assets": len(visual_assets),
            "face_observations": len(face_observations),
            "face_clusters": len(face_clusters),
            "face_identity_candidates": len(face_identity_candidates),
        },
    }
    write_json(output, data)
    return {
        "project": str(project),
        "output": str(output),
        **data["summary"],
    }


def _visible_rows(rows: list[dict[str, Any]], visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if visibility.visible_row(row, evidence_by_id=evidence_by_id)]


def _event_timeline_entry(
    event: dict[str, Any],
    *,
    people_by_event: dict[str, list[dict[str, Any]]],
    places_by_event: dict[str, list[dict[str, Any]]],
    dates_by_event: dict[str, list[dict[str, Any]]],
    assets_by_subject: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    event_id = str(event.get("id") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    assets = assets_by_subject.get(f"event:{event_id}", [])
    return {
        "id": event_id,
        "type": "event",
        "title": str(event.get("title") or "Untitled event"),
        "summary": str(event.get("summary") or ""),
        "start_s": event.get("start_s"),
        "end_s": event.get("end_s"),
        "source_video_ids": _event_source_video_ids(event),
        "event_type": metadata.get("event_type"),
        "relatedness": event.get("relatedness") or metadata.get("relatedness"),
        "confidence": event.get("confidence"),
        "review_status": event.get("review_status") or "unreviewed",
        "people": [_ref("person", row) for row in people_by_event.get(event_id, [])],
        "places": [_place_ref(row) for row in places_by_event.get(event_id, [])],
        "dates": [_date_ref(row) for row in dates_by_event.get(event_id, [])],
        "thumbnail_path": _first_path(assets, "thumbnail_path"),
        "keyframe_path": _first_path(assets, "keyframe_path"),
        "evidence_ids": [str(item) for item in event.get("evidence_ids") or []],
    }


def _scene_timeline_entry(
    scene: dict[str, Any],
    *,
    assets_by_subject: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    scene_id = str(scene.get("id") or "")
    assets = assets_by_subject.get(f"scene:{scene_id}", [])
    return {
        "id": scene_id,
        "type": "scene",
        "source_video_id": scene.get("source_video_id"),
        "index": scene.get("index"),
        "start_s": scene.get("start_s"),
        "end_s": scene.get("end_s"),
        "duration_s": scene.get("duration_s"),
        "scene_type": scene.get("scene_type"),
        "label": scene.get("label"),
        "thumbnail_path": _first_path(assets, "thumbnail_path"),
        "keyframe_path": _first_path(assets, "keyframe_path"),
    }


def _people_track(
    person: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    faces_by_person: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    person_id = str(person.get("id") or "")
    event_entries = _event_entries(person.get("canonical_event_ids") or [], events_by_id)
    faces = faces_by_person.get(person_id, [])
    return {
        "id": person_id,
        "label": str(person.get("label") or ""),
        "aliases": person.get("aliases") or [],
        "kind": person.get("kind"),
        "appearances": event_entries,
        "appearance_count": len(event_entries),
        "thumbnail_path": _first_path(faces, "face_thumbnail_path"),
        "review_status": person.get("review_status") or "unreviewed",
    }


def _place_track(place: dict[str, Any], events_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entries = _event_entries(place.get("canonical_event_ids") or [], events_by_id)
    return {
        "id": str(place.get("id") or ""),
        "label": str(place.get("label") or ""),
        "display_label": _place_display_label(place),
        "normalized_key": _place_normalized_key(place),
        "context": _place_context_identity(place),
        "kind": place.get("kind"),
        "place_type": place.get("place_type"),
        "scope_label": place.get("scope_label"),
        "parent_place_labels": place.get("parent_place_labels") or [],
        "nearby_place_labels": place.get("nearby_place_labels") or [],
        "coordinates": _place_coordinates(place),
        "appearances": entries,
        "appearance_count": len(entries),
        "review_status": place.get("review_status") or "unreviewed",
    }


def _place_context_groups(places: list[dict[str, Any]], events_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for place in places:
        identity = _place_context_identity(place)
        bucket = buckets.setdefault(
            identity["id"],
            {
                **identity,
                "place_ids": [],
                "places": [],
                "event_ids": [],
                "source_video_ids": [],
                "date_years": [],
            },
        )
        if _place_context_basis_rank(identity["basis"]) > _place_context_basis_rank(bucket["basis"]):
            bucket.update({"label": identity["label"], "basis": identity["basis"], "key": identity["key"]})
        place_id = str(place.get("id") or "")
        if place_id and place_id not in bucket["place_ids"]:
            bucket["place_ids"].append(place_id)
            bucket["places"].append(_place_context_place(place, events_by_id))
        for event_id in place.get("canonical_event_ids") or []:
            event_id_text = str(event_id)
            if event_id_text and event_id_text not in bucket["event_ids"]:
                bucket["event_ids"].append(event_id_text)
            event = events_by_id.get(event_id_text)
            if event:
                bucket["source_video_ids"] = _unique_items([*bucket["source_video_ids"], *_event_source_video_ids(event)])
        bucket["source_video_ids"] = _unique_items(
            [*bucket["source_video_ids"], *[str(item) for item in place.get("source_video_ids") or [] if item]]
        )
        bucket["date_years"] = _unique_items([*bucket["date_years"], *_place_date_years(place)])

    groups = []
    for bucket in buckets.values():
        bucket["places"] = sorted(bucket["places"], key=lambda row: row["display_label"].casefold())
        bucket["events"] = _event_entries(bucket["event_ids"], events_by_id)
        bucket["event_count"] = len(bucket["events"])
        bucket["place_count"] = len(bucket["places"])
        groups.append(bucket)
    return sorted(
        groups,
        key=lambda row: (
            _number_or_large((row.get("events") or [{}])[0].get("start_s")),
            str(row.get("label") or "").casefold(),
        ),
    )


def _place_context_place(place: dict[str, Any], events_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    appearances = _event_entries(place.get("canonical_event_ids") or [], events_by_id)
    return {
        "id": str(place.get("id") or ""),
        "label": str(place.get("label") or ""),
        "display_label": _place_display_label(place),
        "kind": place.get("kind"),
        "place_type": place.get("place_type"),
        "appearance_count": len(appearances),
        "first_start_s": appearances[0]["start_s"] if appearances else None,
        "last_end_s": appearances[-1]["end_s"] if appearances else None,
        "review_status": place.get("review_status") or "unreviewed",
        "not_exportable_as_gps": bool(place.get("not_exportable_as_gps", not _place_coordinates(place))),
    }


def _place_context_edges(
    context_edges: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
    edge_metrics_by_edge: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    edges = []
    for edge in context_edges:
        subject_id = str(edge.get("subject_entity_id") or "")
        object_id = str(edge.get("object_entity_id") or "")
        subject = places_by_id.get(subject_id)
        object_ = places_by_id.get(object_id)
        if not subject or not object_:
            continue
        metric = edge_metrics_by_edge.get(str(edge.get("id") or "")) or {}
        metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
        edges.append(
            {
                "id": str(edge.get("id") or ""),
                "source": subject_id,
                "target": object_id,
                "source_label": _place_display_label(subject),
                "target_label": _place_display_label(object_),
                "source_context": _place_context_identity(subject),
                "target_context": _place_context_identity(object_),
                "predicate": str(edge.get("predicate") or ""),
                "label": _place_context_edge_label(edge),
                "weight": metric.get("computed_weight", edge.get("confidence")),
                "confidence": edge.get("confidence"),
                "review_status": edge.get("review_status") or "unreviewed",
                "canonical_event_ids": (edge.get("scope") or {}).get("canonical_event_ids") if isinstance(edge.get("scope"), dict) else [],
                "source_video_ids": (edge.get("scope") or {}).get("source_video_ids") if isinstance(edge.get("scope"), dict) else [],
                "basis": metadata.get("basis") or [],
                "not_exportable_as_gps": bool(metadata.get("not_exportable_as_gps", True)),
            }
        )
    return sorted(edges, key=lambda row: (str(row["predicate"]), str(row["source_label"]), str(row["target_label"])))


def _album_track(
    album: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    assets_by_subject: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    entries = _event_entries(album.get("canonical_event_ids") or [], events_by_id)
    cover_event_id = str(album.get("cover_event_id") or "")
    assets = assets_by_subject.get(f"event:{cover_event_id}", [])
    return {
        "id": str(album.get("id") or ""),
        "title": str(album.get("title") or ""),
        "album_type": album.get("album_type"),
        "date_label": album.get("date_label"),
        "place_label": album.get("place_label"),
        "people_labels": album.get("people_labels") or [],
        "events": entries,
        "thumbnail_path": _first_path(assets, "thumbnail_path"),
        "review_status": album.get("review_status") or "unreviewed",
    }


def _place_node(place: dict[str, Any], events_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    track = _place_track(place, events_by_id)
    return {
        **track,
        "aliases": place.get("aliases") or [],
        "confidence": place.get("confidence"),
        "notes": place.get("notes") or [],
    }


def _person_node(person: dict[str, Any], faces_by_person: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    person_id = str(person.get("id") or "")
    faces = faces_by_person.get(person_id, [])
    return {
        "id": person_id,
        "label": str(person.get("label") or ""),
        "aliases": person.get("aliases") or [],
        "kind": person.get("kind"),
        "canonical_event_ids": person.get("canonical_event_ids") or [],
        "thumbnail_path": _first_path(faces, "face_thumbnail_path"),
        "confidence": person.get("confidence"),
        "review_status": person.get("review_status") or "unreviewed",
        "notes": person.get("notes") or [],
    }


def _graph_nodes(
    people: list[dict[str, Any]],
    places: list[dict[str, Any]],
    context_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for person in people:
        node_id = str(person.get("id") or "")
        if node_id:
            nodes[node_id] = {"id": node_id, "type": "person", "label": str(person.get("label") or node_id)}
    for place in places:
        node_id = str(place.get("id") or "")
        if node_id:
            nodes[node_id] = {"id": node_id, "type": "place", "label": _place_display_label(place)}
    for edge in context_edges:
        for side in ["subject", "object"]:
            node_id = str(edge.get(f"{side}_entity_id") or "")
            if node_id and node_id not in nodes:
                nodes[node_id] = {
                    "id": node_id,
                    "type": str(edge.get(f"{side}_type") or "entity"),
                    "label": str(edge.get(f"{side}_label") or node_id),
                }
    return sorted(nodes.values(), key=lambda row: (row["type"], row["label"].casefold()))


def _graph_edges(
    context_edges: list[dict[str, Any]],
    edge_metrics_by_edge: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    graph_edges = []
    for edge in context_edges:
        metric = edge_metrics_by_edge.get(str(edge.get("id") or "")) or {}
        scope = edge.get("scope") if isinstance(edge.get("scope"), dict) else {}
        graph_edges.append(
            {
                "id": str(edge.get("id") or ""),
                "source": str(edge.get("subject_entity_id") or ""),
                "target": str(edge.get("object_entity_id") or ""),
                "predicate": str(edge.get("predicate") or ""),
                "label": _edge_label(edge),
                "weight": metric.get("computed_weight", edge.get("confidence")),
                "confidence": edge.get("confidence"),
                "review_status": edge.get("review_status") or "unreviewed",
                "canonical_event_ids": scope.get("canonical_event_ids") or [],
                "source_video_ids": scope.get("source_video_ids") or [],
                "supporting_signals": edge.get("supporting_signals") or [],
            }
        )
    return graph_edges


def _relationship_candidate(row: dict[str, Any], events_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    return {
        "id": str(row.get("id") or ""),
        "subject_label": row.get("subject_label"),
        "predicate": row.get("predicate"),
        "object_label": row.get("object_label"),
        "confidence": row.get("confidence"),
        "review_status": row.get("review_status") or "needs_review",
        "supporting_signals": row.get("supporting_signals") or [],
        "events": _event_entries(scope.get("canonical_event_ids") or [], events_by_id),
    }


def _review_queue(
    *,
    events: list[dict[str, Any]],
    people: list[dict[str, Any]],
    places: list[dict[str, Any]],
    dates: list[dict[str, Any]],
    relationship_candidates: list[dict[str, Any]],
    place_context_edges: list[dict[str, Any]],
    face_identity_candidates: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    people_by_id: dict[str, dict[str, Any]],
    face_clusters_by_id: dict[str, dict[str, Any]],
    assets_by_subject: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for candidate in face_identity_candidates:
        if not _needs_review(candidate):
            continue
        cluster = face_clusters_by_id.get(str(candidate.get("face_cluster_id") or "")) or {}
        person = people_by_id.get(str(candidate.get("person_group_id") or "")) or {}
        event_ids = [str(item) for item in candidate.get("supporting_event_ids") or []]
        items.append(
            {
                "task_type": "confirm_face_identity",
                "source_record_type": "face_identity_candidate",
                "source_id": str(candidate.get("id") or ""),
                "title": f"Confirm face identity: {candidate.get('person_label') or person.get('label') or 'unknown person'}",
                "prompt": "Is this face cluster the same person as the candidate people record?",
                "priority": 90,
                "confidence": candidate.get("confidence"),
                "review_status": candidate.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "thumbnail_path": str(cluster.get("thumbnail_path") or ""),
                "candidate": {
                    "face_cluster_id": str(candidate.get("face_cluster_id") or ""),
                    "person_group_id": str(candidate.get("person_group_id") or ""),
                    "person_label": str(candidate.get("person_label") or person.get("label") or ""),
                },
                "actions": ["confirm_identity", "reject_identity", "rename_person", "merge_person"],
            }
        )

    for edge in _place_context_edges_for_review(place_context_edges):
        event_ids = [str(item) for item in edge.get("canonical_event_ids") or []]
        items.append(
            {
                "task_type": "confirm_place_context",
                "source_record_type": "context_edge",
                "source_id": str(edge.get("id") or ""),
                "title": f"Confirm place context: {edge.get('source_label')} -> {edge.get('target_label')}",
                "prompt": "Does this broader/nearby place relationship help locate the footage, or should it remain only a loose clue?",
                "priority": _place_context_review_priority(edge),
                "confidence": edge.get("confidence"),
                "review_status": edge.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "candidate": {
                    "source_place_id": edge.get("source"),
                    "target_place_id": edge.get("target"),
                    "predicate": edge.get("predicate"),
                    "not_exportable_as_gps": edge.get("not_exportable_as_gps"),
                },
                "actions": ["confirm_place_context", "reject_place_context", "confirm_geocode_later"],
            }
        )

    for relationship in relationship_candidates:
        if not _needs_review(relationship):
            continue
        scope = relationship.get("scope") if isinstance(relationship.get("scope"), dict) else {}
        event_ids = [str(item) for item in scope.get("canonical_event_ids") or []]
        items.append(
            {
                "task_type": "confirm_relationship",
                "source_record_type": "relationship_candidate",
                "source_id": str(relationship.get("id") or ""),
                "title": f"Confirm relationship: {relationship.get('subject_label')} -> {relationship.get('object_label')}",
                "prompt": "Is this relationship supported by the tape evidence, or should it remain unconfirmed?",
                "priority": 74,
                "confidence": relationship.get("confidence"),
                "review_status": relationship.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "candidate": {
                    "predicate": relationship.get("predicate"),
                    "subject_label": relationship.get("subject_label"),
                    "object_label": relationship.get("object_label"),
                },
                "actions": ["confirm_relationship", "reject_relationship", "edit_relationship"],
            }
        )

    for place in places:
        if not _needs_review(place):
            continue
        event_ids = [str(item) for item in place.get("canonical_event_ids") or []]
        items.append(
            {
                "task_type": "resolve_place",
                "source_record_type": "place_group",
                "source_id": str(place.get("id") or ""),
                "title": f"Resolve place: {_place_display_label(place)}",
                "prompt": "Confirm what this place label means before it is used as exact location metadata.",
                "priority": 68,
                "confidence": place.get("confidence"),
                "review_status": place.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "candidate": {
                    "label": place.get("label"),
                    "display_label": _place_display_label(place),
                    "place_type": place.get("place_type"),
                    "context": _place_context_identity(place),
                },
                "actions": ["confirm_place", "rename_place", "split_place", "mark_not_location"],
            }
        )

    for person in people:
        if not _needs_review(person):
            continue
        event_ids = [str(item) for item in person.get("canonical_event_ids") or []]
        items.append(
            {
                "task_type": "resolve_person",
                "source_record_type": "people_group",
                "source_id": str(person.get("id") or ""),
                "title": f"Resolve person: {person.get('label')}",
                "prompt": "Confirm whether this name, role, or alias group refers to a real person identity.",
                "priority": 64,
                "confidence": person.get("confidence"),
                "review_status": person.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "candidate": {"label": person.get("label"), "aliases": person.get("aliases") or []},
                "actions": ["confirm_person", "rename_person", "merge_person", "mark_role_only"],
            }
        )

    for event in events:
        if not _needs_review(event):
            continue
        event_id = str(event.get("id") or "")
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        items.append(
            {
                "task_type": "review_event",
                "source_record_type": "event",
                "source_id": event_id,
                "title": f"Review event: {event.get('title')}",
                "prompt": "Confirm the event label, relatedness, people, place, and date before customer-facing export.",
                "priority": 58,
                "confidence": event.get("confidence"),
                "review_status": event.get("review_status") or "needs_review",
                "related_event_ids": [event_id] if event_id else [],
                "events": _event_entries([event_id], events_by_id),
                "thumbnail_path": _first_path(assets_by_subject.get(f"event:{event_id}", []), "thumbnail_path"),
                "candidate": {
                    "title": event.get("title"),
                    "event_type": metadata.get("event_type"),
                    "relatedness": event.get("relatedness") or metadata.get("relatedness"),
                },
                "actions": ["confirm_event", "rename_event", "split_event", "mark_unrelated"],
            }
        )

    for date in dates:
        if _review_closed(date):
            continue
        if not _needs_review(date) and not date.get("excluded_as_event_date"):
            continue
        event_ids = [str(item) for item in date.get("canonical_event_ids") or []]
        items.append(
            {
                "task_type": "resolve_date",
                "source_record_type": "date_group",
                "source_id": str(date.get("id") or ""),
                "title": f"Resolve date: {date.get('label') or date.get('date_value')}",
                "prompt": "Confirm whether this is the recording/event date, historical context, or only a loose clue.",
                "priority": 56,
                "confidence": date.get("confidence"),
                "review_status": date.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "candidate": {
                    "label": date.get("label"),
                    "date_value": date.get("date_value"),
                    "precision": date.get("precision"),
                    "excluded_as_event_date": date.get("excluded_as_event_date"),
                },
                "actions": ["confirm_event_date", "mark_historical_context", "edit_date"],
            }
        )

    ordered = sorted(items, key=_review_item_sort_key)
    for index, item in enumerate(ordered, start=1):
        item["id"] = f"review_item_{index:06d}"
    return ordered


def _media_record(tape: dict[str, Any]) -> dict[str, Any]:
    probe = tape.get("probe") if isinstance(tape.get("probe"), dict) else {}
    return {
        "id": str(tape.get("id") or ""),
        "filename": tape.get("filename"),
        "relative_path": tape.get("relative_path"),
        "duration_s": probe.get("duration_s"),
        "width": (probe.get("video") or {}).get("width") if isinstance(probe.get("video"), dict) else None,
        "height": (probe.get("video") or {}).get("height") if isinstance(probe.get("video"), dict) else None,
    }


def _rows_by_event(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for event_id in row.get("canonical_event_ids") or []:
            by_event[str(event_id)].append(row)
    return dict(by_event)


def _assets_by_subject(assets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in assets:
        key = f"{asset.get('subject_type')}:{asset.get('subject_id')}"
        grouped[key].append(asset)
    return dict(grouped)


def _faces_by_person(faces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for face in faces:
        person_id = str(face.get("person_group_id") or "")
        if person_id:
            grouped[person_id].append(face)
    return dict(grouped)


def _clusters_by_person_candidate(clusters: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cluster in clusters:
        for candidate in cluster.get("candidate_people") or []:
            person_id = str(candidate.get("person_group_id") or "")
            if not person_id:
                continue
            grouped[person_id].append(
                {
                    "face_cluster_id": str(cluster.get("id") or ""),
                    "thumbnail_path": str(cluster.get("thumbnail_path") or ""),
                    "face_count": cluster.get("face_count"),
                    "confidence": candidate.get("confidence"),
                    "review_status": cluster.get("review_status") or "needs_review",
                }
            )
    return dict(grouped)


def _event_entries(event_ids: list[Any], events_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for event_id in event_ids:
        event = events_by_id.get(str(event_id))
        if not event:
            continue
        entries.append(
            {
                "event_id": str(event.get("id") or ""),
                "title": str(event.get("title") or ""),
                "start_s": event.get("start_s"),
                "end_s": event.get("end_s"),
                "source_video_ids": _event_source_video_ids(event),
            }
        )
    return sorted(entries, key=lambda row: _number_or_large(row.get("start_s")))


def _ref(node_type: str, row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row.get("id") or ""), "type": node_type, "label": str(row.get("label") or row.get("title") or "")}


def _place_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row.get("id") or ""), "type": "place", "label": _place_display_label(row)}


def _date_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "type": "date",
        "label": str(row.get("label") or row.get("date_value") or ""),
        "date_value": row.get("date_value"),
        "precision": row.get("precision"),
    }


def _place_display_label(place: dict[str, Any]) -> str:
    label = str(place.get("label") or "Unknown place")
    scope = str(place.get("scope_label") or "").strip()
    parent_labels = [str(item) for item in place.get("parent_place_labels") or [] if item]
    nearby_labels = [str(item) for item in place.get("nearby_place_labels") or [] if item]
    if scope and scope.casefold() != label.casefold() and not scope.casefold().startswith(label.casefold()):
        return f"{label} ({scope})"
    if parent_labels and not _label_contains_any(label, parent_labels):
        return f"{label} ({parent_labels[0]} context)"
    if nearby_labels and not _label_contains_any(label, nearby_labels):
        return f"{label} (near {nearby_labels[0]})"
    return label


def _place_context_identity(place: dict[str, Any]) -> dict[str, str]:
    label = str(place.get("label") or "Unknown place")
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    scope = metadata.get("scope") if isinstance(metadata.get("scope"), dict) else {}
    admin_keys = _string_list(scope.get("admin_context_keys"))
    admin_labels = _string_list(scope.get("admin_context_labels"))
    if admin_keys:
        key_parts = [item.removeprefix("region:") for item in admin_keys]
        key = f"context:{'|'.join(key_parts)}"
        context_label = f"{', '.join(admin_labels[:2])} context" if admin_labels else str(place.get("scope_label") or label)
        basis = "admin_context"
    else:
        parent_labels = _string_list(place.get("parent_place_labels"))
        if parent_labels:
            key = f"context:{'|'.join(_normalize_context_text(item) for item in parent_labels)}"
            context_label = f"{', '.join(parent_labels[:2])} context"
            basis = "parent_place_context"
        else:
            scope_label = str(place.get("scope_label") or "").strip()
            if scope_label:
                key = f"scope:{_normalize_context_text(scope_label)}"
                context_label = scope_label
                basis = "scope_label"
            elif str(place.get("place_type") or "") == "region":
                key = f"context:{_place_normalized_key(place)}"
                context_label = label
                basis = "region_place"
            else:
                key = f"place:{str(place.get('id') or _place_normalized_key(place))}"
                context_label = _place_display_label(place)
                basis = "place"
    return {
        "id": f"place_context_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}",
        "key": key,
        "label": context_label or label,
        "basis": basis,
    }


def _place_context_edge_label(edge: dict[str, Any]) -> str:
    predicate = str(edge.get("predicate") or "").replace("_", " ")
    return f"{edge.get('subject_label')} {predicate} {edge.get('object_label')}"


def _place_context_basis_rank(basis: Any) -> int:
    ranks = {
        "admin_context": 4,
        "parent_place_context": 3,
        "scope_label": 2,
        "region_place": 1,
        "place": 0,
    }
    return ranks.get(str(basis), 0)


def _place_date_years(place: dict[str, Any]) -> list[str]:
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    scope = metadata.get("scope") if isinstance(metadata.get("scope"), dict) else {}
    return [str(item) for item in scope.get("date_years") or [] if item not in (None, "")]


def _place_normalized_key(place: dict[str, Any]) -> str:
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    value = str(metadata.get("normalized_key") or place.get("label") or "")
    return " ".join(value.casefold().replace(",", " ").split())


def _place_coordinates(place: dict[str, Any]) -> dict[str, Any] | None:
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    candidates = [
        place,
        metadata,
        metadata.get("geocode") if isinstance(metadata.get("geocode"), dict) else {},
        metadata.get("selected_geocode") if isinstance(metadata.get("selected_geocode"), dict) else {},
    ]
    for candidate in candidates:
        lat = _number_or_none(candidate.get("lat") or candidate.get("latitude"))
        lng = _number_or_none(candidate.get("lng") or candidate.get("longitude"))
        if lat is not None and lng is not None:
            return {"lat": lat, "lng": lng}
    return None


def _event_source_video_ids(event: dict[str, Any]) -> list[str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    values = []
    for source in [event.get("source_video_ids"), metadata.get("source_video_ids")]:
        if isinstance(source, list):
            values.extend(str(item) for item in source if item)
    for source in [event.get("source_video_id"), metadata.get("source_video_id")]:
        if source:
            values.append(str(source))
    for ranges in [event.get("source_ranges"), metadata.get("source_ranges")]:
        if isinstance(ranges, list):
            values.extend(str(item.get("source_video_id")) for item in ranges if isinstance(item, dict) and item.get("source_video_id"))
    return _unique_items(values)


def _edge_label(edge: dict[str, Any]) -> str:
    predicate = str(edge.get("predicate") or "").replace("_", " ")
    return f"{edge.get('subject_label')} {predicate} {edge.get('object_label')}"


def _place_sort_key(place: dict[str, Any]) -> tuple[str, str]:
    return (str(place.get("kind") or ""), _place_display_label(place).casefold())


def _label_contains_any(label: str, values: list[str]) -> bool:
    label_key = label.casefold()
    return any(value.casefold() in label_key or label_key in value.casefold() for value in values)


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if value not in (None, "")]


def _needs_review(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").casefold() in {"needs_review", "open"}


def _review_closed(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").casefold() in {"confirmed", "rejected", "excluded"}


def _place_context_edges_for_review(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        if not _place_context_edge_reviewable(edge):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target:
            continue
        key = tuple(sorted([source, target]))
        current = buckets.get(key)
        if current is None or _place_context_edge_review_rank(edge) > _place_context_edge_review_rank(current):
            buckets[key] = edge
    return sorted(
        buckets.values(),
        key=lambda row: (
            -_place_context_review_priority(row),
            -(_number_or_none(row.get("confidence")) or 0.0),
            str(row.get("source_label") or "").casefold(),
            str(row.get("target_label") or "").casefold(),
        ),
    )


def _place_context_edge_reviewable(edge: dict[str, Any]) -> bool:
    if _review_closed(edge):
        return False
    predicate = str(edge.get("predicate") or "")
    confidence = _number_or_none(edge.get("confidence")) or 0.0
    if predicate in {"inside_place_candidate", "within_region_candidate"}:
        return True
    if predicate == "same_event_place_context":
        return confidence >= 0.6
    if predicate == "nearby_time_place_context":
        return confidence >= 0.56
    return _needs_review(edge) and bool(edge.get("not_exportable_as_gps"))


def _place_context_edge_review_rank(edge: dict[str, Any]) -> tuple[int, float]:
    predicate_rank = {
        "inside_place_candidate": 4,
        "within_region_candidate": 3,
        "same_event_place_context": 2,
        "nearby_time_place_context": 1,
    }
    return (predicate_rank.get(str(edge.get("predicate") or ""), 0), _number_or_none(edge.get("confidence")) or 0.0)


def _place_context_review_priority(edge: dict[str, Any]) -> int:
    predicate = str(edge.get("predicate") or "")
    if predicate in {"inside_place_candidate", "within_region_candidate"}:
        return 70
    if predicate == "same_event_place_context":
        return 48
    if predicate == "nearby_time_place_context":
        return 40
    return 42


def _review_item_sort_key(row: dict[str, Any]) -> tuple[int, float, float, str]:
    confidence = _number_or_none(row.get("confidence"))
    events = row.get("events") if isinstance(row.get("events"), list) else []
    first_start = _number_or_large(events[0].get("start_s")) if events else 1_000_000_000.0
    return (
        -int(row.get("priority") or 0),
        confidence if confidence is not None else 1.0,
        first_start,
        str(row.get("title") or "").casefold(),
    )


def _normalize_context_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace(",", " ").split())


def _first_path(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        value = row.get(key)
        if value:
            return str(value)
    return ""


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

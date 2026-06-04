from __future__ import annotations

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

    events_by_id = {str(event.get("id")): event for event in events if event.get("id")}
    people_by_event = _rows_by_event(people)
    places_by_event = _rows_by_event(places)
    dates_by_event = _rows_by_event(dates)
    assets_by_subject = _assets_by_subject(visual_assets)
    faces_by_person = _faces_by_person(face_observations)
    edge_metrics_by_edge = {str(metric.get("edge_id")): metric for metric in edge_metrics if metric.get("edge_id")}

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
        "people": [_person_node(person, faces_by_person) for person in sorted(people, key=lambda row: str(row.get("label") or "").casefold())],
        "relationships": {
            "nodes": _graph_nodes(people, places, context_edges),
            "edges": _graph_edges(context_edges, edge_metrics_by_edge),
            "candidates": [_relationship_candidate(row, events_by_id) for row in relationships],
        },
        "assets": {
            "visual": visual_assets,
            "faces": face_observations,
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
            "visual_assets": len(visual_assets),
            "face_observations": len(face_observations),
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

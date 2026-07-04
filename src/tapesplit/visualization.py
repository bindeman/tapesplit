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
    event_alignments = _visible_rows(read_jsonl(project / "event_alignments.jsonl"), visibility, evidence_by_id)
    event_reconciliations = _visible_rows(read_jsonl(project / "event_reconciliations.jsonl"), visibility, evidence_by_id)
    relationships = _visible_rows(read_jsonl(project / "relationship_candidates.jsonl"), visibility, evidence_by_id)
    place_roles = read_jsonl(project / "event_place_roles.jsonl")
    event_continuity_contexts = read_jsonl(project / "event_continuity_contexts.jsonl")
    visual_assets = read_jsonl(project / "visual_assets.jsonl")
    face_observations = read_jsonl(project / "face_observations.jsonl")
    face_clusters = read_jsonl(project / "face_clusters.jsonl")
    face_identity_candidates = read_jsonl(project / "face_identity_candidates.jsonl")
    speaker_segments = read_jsonl(project / "speaker_segments.jsonl")
    speaker_identity_candidates = read_jsonl(project / "speaker_identity_candidates.jsonl")

    events_by_id = {str(event.get("id")): event for event in events if event.get("id")}
    event_alignments_by_event = {
        str(row.get("canonical_event_id")): row
        for row in event_alignments
        if row.get("canonical_event_id")
    }
    event_reconciliations_by_event = {
        str(row.get("canonical_event_id")): row
        for row in event_reconciliations
        if row.get("canonical_event_id")
    }
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
    video_offsets = _video_offsets(tapes)
    place_contexts = _place_context_groups(places, events_by_id)
    place_context_edges = _place_context_edges(context_edges, places_by_id, edge_metrics_by_edge)
    review_queues = _review_queues(
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
        speaker_identity_candidates=speaker_identity_candidates,
        assets_by_subject=assets_by_subject,
        video_offsets=video_offsets,
        event_alignments_by_event=event_alignments_by_event,
    )
    review_queue = review_queues["review_queue"]
    review_backlog = review_queues["review_backlog"]

    data = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "project": str(project),
        "media": [_media_record(tape, offset_s=video_offsets.get(str(tape.get("id") or ""), 0.0)) for tape in tapes],
        "timeline": {
            "events": [
                _event_timeline_entry(
                    event,
                    people_by_event=people_by_event,
                    places_by_event=places_by_event,
                    dates_by_event=dates_by_event,
                    assets_by_subject=assets_by_subject,
                    event_alignments_by_event=event_alignments_by_event,
                    event_reconciliations_by_event=event_reconciliations_by_event,
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
            "speakers": _speaker_tracks(speaker_segments, speaker_identity_candidates, people_by_id),
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
            "candidates": [
                _relationship_candidate(row, events_by_id, events=events, video_offsets=video_offsets)
                for row in relationships
            ],
            "place_context_edges": place_context_edges,
            "face_identity_candidates": face_identity_candidates,
        },
        "review_queue": review_queue,
        "review_backlog": review_backlog,
        "assets": {
            "visual": visual_assets,
            "faces": face_observations,
            "face_clusters": face_clusters,
            "face_identity_candidates": face_identity_candidates,
            "place_roles": place_roles,
            "event_continuity_contexts": event_continuity_contexts,
            "event_alignments": event_alignments,
            "event_reconciliations": event_reconciliations,
            "speaker_segments": speaker_segments,
            "speaker_identity_candidates": speaker_identity_candidates,
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
            "review_backlog_items": len(review_backlog),
            "review_total_items": len(review_queue) + len(review_backlog),
            "suggested_review_actions": sum(1 for item in [*review_queue, *review_backlog] if item.get("suggested_action")),
            "visual_assets": len(visual_assets),
            "face_observations": len(face_observations),
            "face_clusters": len(face_clusters),
            "face_identity_candidates": len(face_identity_candidates),
            "event_place_roles": len(place_roles),
            "event_continuity_contexts": len(event_continuity_contexts),
            "event_alignments": len(event_alignments),
            "event_reconciliations": len(event_reconciliations),
            "speaker_segments": len(speaker_segments),
            "speaker_tracks": len({str(row.get("speaker_label")) for row in speaker_segments if row.get("speaker_label")}),
            "speaker_identity_candidates": len(speaker_identity_candidates),
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
    event_alignments_by_event: dict[str, dict[str, Any]],
    event_reconciliations_by_event: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    event_id = str(event.get("id") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    assets = assets_by_subject.get(f"event:{event_id}", [])
    reconciliation = _event_reconciliation_summary(event_reconciliations_by_event.get(event_id))
    return {
        "id": event_id,
        "type": "event",
        "title": str((reconciliation or {}).get("reconciled_title") or event.get("title") or "Untitled event"),
        "original_title": str(event.get("title") or "Untitled event"),
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
        "thumbnail_path": _representative_path(assets, "thumbnail_path"),
        "keyframe_path": _representative_path(assets, "keyframe_path"),
        "evidence_ids": [str(item) for item in event.get("evidence_ids") or []],
        "alignment": _event_alignment_summary(event_alignments_by_event.get(event_id)),
        "reconciliation": reconciliation,
    }


def _event_alignment_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    evidence_claims = row.get("evidence_claims") if isinstance(row.get("evidence_claims"), list) else []
    context_anchors = row.get("transcript_context_anchors") if isinstance(row.get("transcript_context_anchors"), list) else []
    suggested_ranges = row.get("suggested_source_ranges") if isinstance(row.get("suggested_source_ranges"), list) else []
    return {
        "id": str(row.get("id") or ""),
        "timing_status": row.get("timing_status"),
        "support_score": row.get("support_score"),
        "warnings": row.get("warnings") or [],
        "signals": row.get("signals") or [],
        "suggested_review_status": row.get("suggested_review_status"),
        "transcript_support_count": len(row.get("transcript_support") or []),
        "alternate_anchor_count": len(row.get("alternate_transcript_anchors") or []),
        "evidence_claim_statuses": _count_values(item.get("status") for item in evidence_claims if isinstance(item, dict)),
        "transcript_context_anchors": [
            {
                "label": item.get("label"),
                "role": item.get("role"),
                "confidence": item.get("confidence"),
                "distance_to_event_s": item.get("distance_to_event_s"),
                "text": item.get("text"),
            }
            for item in context_anchors[:6]
            if isinstance(item, dict)
        ],
        "suggested_source_ranges": suggested_ranges[:3],
    }


def _count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _event_reconciliation_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": str(row.get("id") or ""),
        "reconciled_title": row.get("reconciled_title"),
        "original_title": row.get("original_title"),
        "reconciled_summary": row.get("reconciled_summary"),
        "original_summary": row.get("original_summary"),
        "title_status": row.get("title_status"),
        "reconciliation_status": row.get("reconciliation_status"),
        "confidence": row.get("confidence"),
        "review_status": row.get("review_status"),
        "selected_place_labels": row.get("selected_place_labels") or [],
        "rejected_place_labels": row.get("rejected_place_labels") or [],
        "selected_source_ranges": (row.get("selected_source_ranges") or [])[:3],
        "relocated_evidence_ranges": (row.get("relocated_evidence_ranges") or [])[:3],
        "warnings": row.get("warnings") or [],
        "signals": row.get("signals") or [],
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


def _speaker_tracks(
    speaker_segments: list[dict[str, Any]],
    speaker_identity_candidates: list[dict[str, Any]],
    people_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for segment in speaker_segments:
        label = str(segment.get("speaker_label") or "").strip()
        if label:
            grouped[label].append(segment)
    identity_by_label = _speaker_identity_candidates_by_label(speaker_identity_candidates)
    tracks = []
    for label, segments in grouped.items():
        sorted_segments = sorted(segments, key=lambda row: (str(row.get("source_video_id") or ""), _number_or_large(row.get("start_s"))))
        total_duration = sum(
            max(0.0, float(segment.get("end_s") or 0.0) - float(segment.get("start_s") or 0.0))
            for segment in sorted_segments
        )
        tracks.append(
            {
                "id": label,
                "label": label,
                "segment_count": len(sorted_segments),
                "source_video_ids": sorted({str(segment.get("source_video_id")) for segment in sorted_segments if segment.get("source_video_id")}),
                "start_s": min((_number_or_large(segment.get("start_s")) for segment in sorted_segments), default=None),
                "end_s": max((float(segment.get("end_s") or 0.0) for segment in sorted_segments), default=None),
                "total_duration_s": round(total_duration, 3),
                "identity_candidates": [
                    _speaker_identity_candidate_for_review(candidate, people_by_id)
                    for candidate in identity_by_label.get(label, [])[:5]
                ],
                "segments": [
                    {
                        "id": str(segment.get("id") or ""),
                        "source_video_id": segment.get("source_video_id"),
                        "start_s": segment.get("start_s"),
                        "end_s": segment.get("end_s"),
                        "confidence": segment.get("confidence"),
                        "provider": segment.get("provider"),
                        "model": segment.get("model"),
                    }
                    for segment in sorted_segments
                ],
            }
        )
    return sorted(tracks, key=lambda row: (-int(row.get("segment_count") or 0), str(row.get("label") or "")))


def _speaker_identity_candidates_by_label(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        if _review_closed(candidate):
            continue
        speaker_label = str(candidate.get("speaker_label") or "")
        if speaker_label:
            grouped[speaker_label].append(candidate)
    return {
        speaker_label: sorted(rows, key=lambda row: (_number_or_none(row.get("confidence")) or 0.0), reverse=True)
        for speaker_label, rows in grouped.items()
    }


def _speaker_identity_candidate_for_review(
    candidate: dict[str, Any],
    people_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    person = people_by_id.get(str(candidate.get("person_group_id") or "")) or {}
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    speaker_segment_ids = [str(item) for item in candidate.get("speaker_segment_ids") or []]
    transcript_segment_ids = [str(item) for item in candidate.get("transcript_segment_ids") or []]
    return {
        "speaker_identity_candidate_id": str(candidate.get("id") or ""),
        "speaker_label": str(candidate.get("speaker_label") or ""),
        "person_group_id": str(candidate.get("person_group_id") or ""),
        "person_label": str(candidate.get("person_label") or person.get("label") or ""),
        "person_kind": candidate.get("person_kind") or person.get("kind"),
        "confidence": candidate.get("confidence"),
        "supporting_event_ids": [str(item) for item in candidate.get("canonical_event_ids") or []],
        "relationship_candidate_ids": [str(item) for item in candidate.get("relationship_candidate_ids") or []],
        "face_cluster_ids": [str(item) for item in candidate.get("face_cluster_ids") or []],
        "speaker_segment_count": len(speaker_segment_ids),
        "speaker_segment_ids": speaker_segment_ids[:20],
        "transcript_segment_count": len(transcript_segment_ids),
        "transcript_segment_ids": transcript_segment_ids[:20],
        "supporting_signals": [str(item) for item in candidate.get("supporting_signals") or []],
        "basis": [str(item) for item in candidate.get("basis") or []],
        "text_examples": [str(item) for item in candidate.get("text_examples") or []],
        "mentioned_people": candidate.get("mentioned_people") or [],
        "signal_sources": metadata.get("signal_sources") if isinstance(metadata.get("signal_sources"), dict) else {},
        "review_status": candidate.get("review_status") or "needs_review",
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
        "evidence_basis": _place_evidence_basis(place),
        "location_options": _place_location_options(place),
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
                "source_kind": subject.get("kind"),
                "target_kind": object_.get("kind"),
                "source_place_type": subject.get("place_type"),
                "target_place_type": object_.get("place_type"),
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


def _relationship_candidate(
    row: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    *,
    events: list[dict[str, Any]],
    video_offsets: dict[str, float],
) -> dict[str, Any]:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    event_ids = _scope_event_ids(scope, events=events, video_offsets=video_offsets)
    return {
        "id": str(row.get("id") or ""),
        "subject_entity_id": row.get("subject_entity_id"),
        "subject_label": row.get("subject_label"),
        "predicate": row.get("predicate"),
        "object_entity_id": row.get("object_entity_id"),
        "object_label": row.get("object_label"),
        "confidence": row.get("confidence"),
        "review_status": row.get("review_status") or "needs_review",
        "supporting_signals": row.get("supporting_signals") or [],
        "events": _event_entries(event_ids, events_by_id),
    }


def _review_queues(
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
    speaker_identity_candidates: list[dict[str, Any]],
    assets_by_subject: dict[str, list[dict[str, Any]]],
    video_offsets: dict[str, float],
    event_alignments_by_event: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    event_alignments_by_event = event_alignments_by_event or {}
    role_identity_options_by_person = _role_identity_options_by_person(people, relationship_candidates, events_by_id)

    for cluster_id, candidates in _face_identity_candidates_by_cluster(face_identity_candidates).items():
        cluster = face_clusters_by_id.get(cluster_id) or {}
        if _review_closed(cluster):
            continue
        review_candidates = [candidate for candidate in candidates if not _review_closed(candidate)]
        if not review_candidates:
            continue
        top_candidate = review_candidates[0]
        event_ids = _unique_items(
            [
                str(event_id)
                for candidate in review_candidates
                for event_id in candidate.get("supporting_event_ids") or []
            ]
        )
        items.append(
            {
                "task_type": "resolve_face_cluster",
                "source_record_type": "face_cluster",
                "source_id": cluster_id,
                "title": f"Resolve face cluster: {top_candidate.get('person_label') or 'unknown person'}",
                "prompt": "Which candidate person, if any, matches this face cluster?",
                "priority": 90,
                "confidence": top_candidate.get("confidence"),
                "review_status": cluster.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "thumbnail_path": str(cluster.get("thumbnail_path") or ""),
                "candidate": {
                    "face_cluster_id": cluster_id,
                    "face_count": cluster.get("face_count"),
                    "identity_candidates": [
                        _face_identity_candidate_for_review(candidate, people_by_id)
                        for candidate in review_candidates[:5]
                    ],
                },
                "actions": ["confirm_identity", "reject_identity", "rename_person", "merge_person", "mark_role_only"],
            }
        )

    for speaker_label, candidates in _speaker_identity_candidates_by_label(speaker_identity_candidates).items():
        review_candidates = [candidate for candidate in candidates if not _review_closed(candidate)]
        if not review_candidates:
            continue
        top_candidate = review_candidates[0]
        event_ids = _unique_items(
            [
                str(event_id)
                for candidate in review_candidates
                for event_id in candidate.get("canonical_event_ids") or []
            ]
        )
        items.append(
            {
                "task_type": "resolve_speaker",
                "source_record_type": "speaker_identity_candidate",
                "source_id": str(top_candidate.get("id") or ""),
                "title": f"Resolve speaker: {speaker_label}",
                "prompt": "Which person, if any, is this recurring local speaker voice?",
                "priority": 82,
                "confidence": top_candidate.get("confidence"),
                "review_status": top_candidate.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "thumbnail_path": _event_thumbnail_path(event_ids, assets_by_subject),
                "candidate": {
                    "speaker_label": speaker_label,
                    "speaker_segment_count": len(top_candidate.get("speaker_segment_ids") or []),
                    "identity_candidates": [
                        _speaker_identity_candidate_for_review(candidate, people_by_id)
                        for candidate in review_candidates[:5]
                    ],
                },
                "actions": ["confirm_speaker_identity", "reject_speaker_identity"],
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
                "thumbnail_path": _event_thumbnail_path(event_ids, assets_by_subject),
                "candidate": {
                    "source_place_id": edge.get("source"),
                    "target_place_id": edge.get("target"),
                    "source_label": edge.get("source_label"),
                    "target_label": edge.get("target_label"),
                    "predicate": edge.get("predicate"),
                    "source_kind": edge.get("source_kind"),
                    "target_kind": edge.get("target_kind"),
                    "source_place_type": edge.get("source_place_type"),
                    "target_place_type": edge.get("target_place_type"),
                    "not_exportable_as_gps": edge.get("not_exportable_as_gps"),
                },
                "actions": ["confirm_place_context", "reject_place_context", "confirm_geocode_later"],
            }
        )

    for relationship_group in _relationship_review_groups(relationship_candidates):
        relationship = max(
            relationship_group,
            key=lambda row: _number_or_none(row.get("confidence")) or 0.0,
        )
        relationship_ids = [str(row.get("id") or "") for row in relationship_group if row.get("id")]
        event_ids = _unique_items(
            [
                event_id
                for row in relationship_group
                for event_id in _scope_event_ids(
                    row.get("scope") if isinstance(row.get("scope"), dict) else {},
                    events=events,
                    video_offsets=video_offsets,
                )
            ]
        )
        title_suffix = f" ({len(relationship_group)} observations)" if len(relationship_group) > 1 else ""
        items.append(
            {
                "task_type": "confirm_relationship",
                "source_record_type": "relationship_candidate",
                "source_id": str(relationship.get("id") or ""),
                "title": f"Confirm relationship: {relationship.get('subject_label')} -> {relationship.get('object_label')}{title_suffix}",
                "prompt": "Is this relationship supported by the tape evidence, or should it remain unconfirmed?",
                "priority": 74,
                "confidence": relationship.get("confidence"),
                "review_status": relationship.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "thumbnail_path": _event_thumbnail_path(event_ids, assets_by_subject),
                "candidate": {
                    "predicate": relationship.get("predicate"),
                    "subject_entity_id": relationship.get("subject_entity_id"),
                    "subject_label": relationship.get("subject_label"),
                    "object_entity_id": relationship.get("object_entity_id"),
                    "object_label": relationship.get("object_label"),
                    "supporting_signals": _unique_items(
                        [
                            str(signal)
                            for row in relationship_group
                            for signal in row.get("supporting_signals") or []
                            if signal
                        ]
                    ),
                    "relationship_ids": relationship_ids,
                    "relationship_count": len(relationship_ids),
                    "relationship_candidates": [
                        {
                            "id": str(row.get("id") or ""),
                            "confidence": row.get("confidence"),
                            "event_ids": _scope_event_ids(
                                row.get("scope") if isinstance(row.get("scope"), dict) else {},
                                events=events,
                                video_offsets=video_offsets,
                            ),
                        }
                        for row in relationship_group
                    ],
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
                "prompt": "This appears to be a place/context clue. Confirm the best label or choose Other before it is used as exact location metadata.",
                "priority": 68,
                "confidence": place.get("confidence"),
                "review_status": place.get("review_status") or "needs_review",
                "related_event_ids": event_ids,
                "events": _event_entries(event_ids, events_by_id),
                "thumbnail_path": _event_thumbnail_path(event_ids, assets_by_subject),
                "candidate": {
                    "label": place.get("label"),
                    "display_label": _place_display_label(place),
                    "kind": place.get("kind"),
                    "place_type": place.get("place_type"),
                    "context": _place_context_identity(place),
                    "coordinates": _place_coordinates(place),
                    "evidence_basis": _place_evidence_basis(place),
                    "location_options": _place_location_options(place),
                },
                "actions": ["confirm_place", "rename_place", "split_place", "mark_not_location"],
            }
        )

    for person in people:
        if not _needs_review(person):
            continue
        event_ids = [str(item) for item in person.get("canonical_event_ids") or []]
        role_identity_options = role_identity_options_by_person.get(str(person.get("id") or ""), [])
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
                "candidate": {
                    "label": person.get("label"),
                    "aliases": person.get("aliases") or [],
                    "kind": person.get("kind"),
                    "event_count": len(event_ids),
                    "role_identity_options": role_identity_options,
                },
                "actions": ["confirm_person", "rename_person", "merge_person", "mark_role_only"],
            }
        )

    for event in events:
        event_id = str(event.get("id") or "")
        unverified = _description_unverified(event_alignments_by_event.get(event_id))
        if not _needs_review(event) and not (unverified and not _review_closed(event)):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        items.append(
            {
                "task_type": "review_event",
                "source_record_type": "event",
                "source_id": event_id,
                "title": (
                    f"Verify footage matches: {event.get('title')}"
                    if unverified
                    else f"Review event: {event.get('title')}"
                ),
                "prompt": (
                    "The tape's own transcript does not support this description — the model may have "
                    "mislabeled this footage. Play the range and rename or confirm."
                    if unverified
                    else "Confirm the event label, relatedness, people, place, and date before customer-facing export."
                ),
                "priority": 75 if unverified else 58,
                "confidence": event.get("confidence"),
                "review_status": event.get("review_status") or "needs_review",
                "review_reason": "description not supported by transcript" if unverified else None,
                "related_event_ids": [event_id] if event_id else [],
                "events": _event_entries([event_id], events_by_id),
                "thumbnail_path": _first_path(assets_by_subject.get(f"event:{event_id}", []), "thumbnail_path"),
                "candidate": {
                    "title": event.get("title"),
                    "event_type": metadata.get("event_type"),
                    "relatedness": event.get("relatedness") or metadata.get("relatedness"),
                    "unverified_description": unverified,
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

    return _partition_review_items(items)


def _partition_review_items(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    primary: list[dict[str, Any]] = []
    backlog: list[dict[str, Any]] = []
    for item in sorted(items, key=_review_item_sort_key):
        tier, reason = _review_item_tier(item)
        item["review_tier"] = tier
        item["review_reason"] = reason
        suggestion = _suggested_review_action(item)
        if suggestion:
            item["suggested_action"] = suggestion
        if tier == "primary":
            primary.append(item)
        else:
            backlog.append(item)

    for index, item in enumerate(primary, start=1):
        item["id"] = f"review_item_{index:06d}"
    for index, item in enumerate(backlog, start=1):
        item["id"] = f"review_backlog_item_{index:06d}"
    return {"review_queue": primary, "review_backlog": backlog}


def _review_item_tier(item: dict[str, Any]) -> tuple[str, str]:
    task_type = str(item.get("task_type") or "")
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}

    if task_type == "resolve_face_cluster":
        return _face_review_tier(candidate)
    if task_type == "resolve_speaker":
        return _speaker_review_tier(item, candidate)
    if task_type == "confirm_relationship":
        return "primary", "Relationship candidates change the people graph and should be explicitly confirmed."
    if task_type == "confirm_place_context":
        return _place_context_review_tier(item, candidate)
    if task_type == "resolve_place":
        return _place_review_tier(candidate)
    if task_type == "resolve_person":
        return _person_review_tier(candidate)
    if task_type == "review_event":
        return _event_review_tier(item, candidate)
    if task_type == "resolve_date":
        return _date_review_tier(candidate)
    return "backlog", "Useful cleanup, but not required for the first-pass story."


def _face_review_tier(candidate: dict[str, Any]) -> tuple[str, str]:
    face_count = int(_number_or_none(candidate.get("face_count")) or 0)
    identity_candidates = candidate.get("identity_candidates") if isinstance(candidate.get("identity_candidates"), list) else []
    top_candidate = identity_candidates[0] if identity_candidates and isinstance(identity_candidates[0], dict) else {}
    quality = str(top_candidate.get("face_quality_status") or "").casefold()
    if _face_significance_score(candidate) <= 0:
        return (
            "backlog",
            "Appears to be a background face (one-off appearance in a crowd scene); "
            "not worth resolving unless this person matters to the family story.",
        )
    if face_count >= 2 and quality != "low_quality":
        return "primary", "Usable multi-face cluster; resolving it improves people timelines."
    if (_number_or_none(top_candidate.get("direct_name_strength")) or 0.0) >= 0.9 and quality not in {"low_quality", "unusable"}:
        return "primary", "Strong direct-name face match."
    return "backlog", "Low-quality or singleton face crop; keep as evidence but do not interrupt first-pass review."


def _face_significance_score(candidate: dict[str, Any]) -> float:
    """How much this face matters to the family narrative.

    Recurrence across events, being named on tape, and appearing in
    small-group scenes make a face significant; a single appearance inside a
    crowded scene (party guests, audiences) makes it background noise that
    should not demand review.
    """

    face_count = int(_number_or_none(candidate.get("face_count")) or 0)
    identity_candidates = candidate.get("identity_candidates") if isinstance(candidate.get("identity_candidates"), list) else []
    event_ids: set[str] = set()
    direct_name = 0.0
    crowd = None
    for row in identity_candidates:
        if not isinstance(row, dict):
            continue
        for event_id in row.get("supporting_event_ids") or []:
            event_ids.add(str(event_id))
        direct_name = max(direct_name, _number_or_none(row.get("direct_name_strength")) or 0.0)
        people_count = _number_or_none(row.get("average_event_people_count"))
        if people_count is not None:
            crowd = people_count if crowd is None else min(crowd, people_count)

    score = 0.0
    if len(event_ids) >= 2:
        score += 2.0
    if face_count >= 3:
        score += 1.0
    if face_count >= 6:
        score += 1.0
    if direct_name >= 0.9:
        score += 3.0
    if crowd is not None and crowd <= 4:
        score += 1.0
    if crowd is not None and crowd >= 7 and len(event_ids) <= 1 and face_count <= 2:
        score -= 2.0
    if face_count <= 1 and len(event_ids) <= 1 and direct_name < 0.9:
        score -= 1.0
    return score


def _speaker_review_tier(item: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, str]:
    identity_candidates = candidate.get("identity_candidates") if isinstance(candidate.get("identity_candidates"), list) else []
    top_candidate = identity_candidates[0] if identity_candidates and isinstance(identity_candidates[0], dict) else {}
    confidence = _number_or_none(top_candidate.get("confidence")) or _number_or_none(item.get("confidence")) or 0.0
    sources = top_candidate.get("signal_sources") if isinstance(top_candidate.get("signal_sources"), dict) else {}
    if confidence >= 0.68 and (
        sources.get("self_identification_phrase") or sources.get("role_identity_bridge")
    ):
        return "primary", "Speaker identity has a strong name/role bridge and improves narrator/person timelines."
    if confidence >= 0.55:
        return "primary", "Recurring speaker candidate is strong enough to review before export."
    return "backlog", "Useful speaker context, but lower confidence than faces, dates, or relationship decisions."


def _place_context_review_tier(item: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, str]:
    predicate = str(candidate.get("predicate") or "")
    confidence = _number_or_none(item.get("confidence")) or 0.0
    source_kind = str(candidate.get("source_kind") or "")
    target_kind = str(candidate.get("target_kind") or "")
    if predicate == "inside_place_candidate" and target_kind == "named_place_candidate" and confidence >= 0.7:
        return "primary", "Named containing place could materially improve location grouping."
    if predicate == "within_region_candidate" and source_kind == "named_place_candidate" and confidence >= 0.72:
        return "primary", "Named place-to-region link is export-relevant context."
    return "backlog", "Loose place context remains selectable, but continuity can default it without blocking review."


def _place_review_tier(candidate: dict[str, Any]) -> tuple[str, str]:
    kind = str(candidate.get("kind") or "")
    place_type = str(candidate.get("place_type") or "")
    evidence_basis = candidate.get("evidence_basis") if isinstance(candidate.get("evidence_basis"), dict) else {}
    source_label = str(evidence_basis.get("source_label") or "")
    role_counts = evidence_basis.get("role_counts") if isinstance(evidence_basis.get("role_counts"), dict) else {}
    if kind == "named_place_candidate" and (source_label == "from direct mention" or place_type == "region"):
        return "primary", "Directly mentioned named place can anchor the broader tape context."
    if "generic_scene_type" in role_counts:
        return "backlog", "Generic scene type is useful for search/grouping, but not exact location metadata."
    return "backlog", "Place clue is preserved as editable context, not a primary decision."


def _person_review_tier(candidate: dict[str, Any]) -> tuple[str, str]:
    kind = str(candidate.get("kind") or "")
    aliases = [str(alias) for alias in candidate.get("aliases") or [] if alias]
    role_identity_options = candidate.get("role_identity_options") if isinstance(candidate.get("role_identity_options"), list) else []
    if kind == "role_candidate":
        if role_identity_options:
            return "primary", "Role mention can be merged into a named person using relationship and event context."
        return "backlog", "Role-only mention should stay editable but usually needs more evidence before interrupting review."
    if len(_unique_items(aliases)) >= 2:
        return "primary", "Alias/nickname merge affects the person graph across multiple clips."
    return "backlog", "Single-name person candidate can remain passive until tied to stronger face or relationship evidence."


def _event_review_tier(item: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, str]:
    relatedness = str(candidate.get("relatedness") or "").casefold()
    confidence = _number_or_none(item.get("confidence")) or 0.0
    if relatedness and relatedness not in {"likely_family", "family", "personal"}:
        return "primary", "Relatedness is uncertain and may affect whether this event belongs in the family story."
    if confidence < 0.75:
        return "primary", "Low-confidence event label should be checked before export."
    return "backlog", "High-confidence likely-family event; default it and let the timeline editor handle optional cleanup."


def _date_review_tier(candidate: dict[str, Any]) -> tuple[str, str]:
    if candidate.get("excluded_as_event_date"):
        return "backlog", "Historical or loose date clue; useful context but not an event-date blocker."
    precision = str(candidate.get("precision") or "")
    if precision in {"day", "month"} and candidate.get("date_value"):
        return "primary", "Specific date candidate can affect album/export metadata."
    return "backlog", "Loose date clue should remain editable without blocking first-pass review."


def _suggested_review_action(item: dict[str, Any]) -> dict[str, Any]:
    task_type = str(item.get("task_type") or "")
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    confidence = _number_or_none(item.get("confidence"))

    if task_type == "resolve_face_cluster":
        identity_candidates = candidate.get("identity_candidates") if isinstance(candidate.get("identity_candidates"), list) else []
        top = identity_candidates[0] if identity_candidates and isinstance(identity_candidates[0], dict) else {}
        person_label = str(top.get("person_label") or "").strip()
        face_identity_candidate_id = str(top.get("face_identity_candidate_id") or "").strip()
        person_group_id = str(top.get("person_group_id") or "").strip()
        if not person_label or not face_identity_candidate_id or not person_group_id:
            return {}
        rationale_bits = [
            f"Top candidate from {top.get('candidate_ambiguity') or 'unknown'}-ambiguity face/event evidence.",
            "Directly named in an overlapping event." if top.get("direct_name_event_ids") else "Based on event co-occurrence; user can change it.",
        ]
        if str(top.get("face_quality_status") or "") == "low_quality":
            rationale_bits.append("Face crop is low quality, so this should stay easy to correct.")
        return _suggestion(
            action="confirm_identity",
            target_id=face_identity_candidate_id,
            target_type="face_identity_candidate",
            label=f"Appears to be {person_label}",
            rationale=" ".join(rationale_bits),
            confidence=_number_or_none(top.get("confidence")) or confidence,
            payload={"face_cluster_id": item.get("source_id"), "person_group_id": person_group_id},
        )

    if task_type == "resolve_speaker":
        identity_candidates = candidate.get("identity_candidates") if isinstance(candidate.get("identity_candidates"), list) else []
        top = identity_candidates[0] if identity_candidates and isinstance(identity_candidates[0], dict) else {}
        speaker_label = str(candidate.get("speaker_label") or item.get("title") or "speaker")
        person_label = str(top.get("person_label") or "").strip()
        speaker_identity_candidate_id = str(top.get("speaker_identity_candidate_id") or "").strip()
        person_group_id = str(top.get("person_group_id") or "").strip()
        if not speaker_identity_candidate_id or not person_group_id or not person_label:
            return {}
        return _suggestion(
            action="confirm_speaker_identity",
            target_id=speaker_identity_candidate_id,
            target_type="speaker_identity_candidate",
            label=f"{speaker_label} appears to be {person_label}",
            rationale="Best current speaker/person guess from role, relationship, face, and transcript context. Keep editable because voice labels are still draft.",
            confidence=_number_or_none(top.get("confidence")) or confidence,
            payload={
                "speaker_label": candidate.get("speaker_label"),
                "person_group_id": person_group_id,
            },
        )

    if task_type == "confirm_relationship":
        subject = str(candidate.get("subject_label") or "Unknown person")
        object_ = str(candidate.get("object_label") or "unknown person")
        predicate = str(candidate.get("predicate") or "relationship_candidate")
        relationship_ids = [str(item) for item in candidate.get("relationship_ids") or [] if item]
        return _suggestion(
            action="confirm_relationship",
            target_id=str(item.get("source_id") or ""),
            target_type=str(item.get("source_record_type") or "relationship_candidate"),
            label=f"Appears to link {subject} to {object_} as {_humanize_token(predicate)}",
            rationale="Relationship candidate is supported by transcript/context signals; keep it as the default unless a reviewer corrects it.",
            confidence=confidence,
            payload={
                "predicate": predicate,
                "subject_entity_id": candidate.get("subject_entity_id"),
                "subject_label": candidate.get("subject_label"),
                "object_entity_id": candidate.get("object_entity_id"),
                "object_label": candidate.get("object_label"),
                "relationship_ids": relationship_ids,
            },
        )

    if task_type == "confirm_place_context":
        source = str(candidate.get("source_label") or "Source place")
        target = str(candidate.get("target_label") or "context")
        predicate = str(candidate.get("predicate") or "place_context")
        return _suggestion(
            action="confirm_place_context",
            target_id=str(item.get("source_id") or ""),
            target_type=str(item.get("source_record_type") or "context_edge"),
            label=f"Use {target} as context for {source}",
            rationale="Best current place context from direct mention, continuity, or same-event evidence; not treated as GPS unless later confirmed.",
            confidence=confidence,
            payload={"exportable_as_gps": False, "predicate": predicate},
        )

    if task_type == "resolve_place":
        options = candidate.get("location_options") if isinstance(candidate.get("location_options"), list) else []
        selected = next((option for option in options if isinstance(option, dict) and option.get("selected")), None)
        if selected is None and options and isinstance(options[0], dict):
            selected = options[0]
        label = str((selected or {}).get("label") or candidate.get("label") or "").strip()
        display_label = str((selected or {}).get("display_label") or candidate.get("display_label") or label)
        scope = str((selected or {}).get("scope_label") or (candidate.get("context") or {}).get("label") or "")
        place_type = str(candidate.get("place_type") or "")
        evidence_basis = candidate.get("evidence_basis") if isinstance(candidate.get("evidence_basis"), dict) else {}
        source_label = str((selected or {}).get("source_label") or evidence_basis.get("source_label") or "from context")
        exportable_as_gps = bool(candidate.get("coordinates")) and str(candidate.get("kind") or "") == "named_place_candidate"
        return _suggestion(
            action="confirm_place",
            target_id=str(item.get("source_id") or ""),
            target_type=str(item.get("source_record_type") or "place_group"),
            label=f"Use {display_label}",
            rationale=f"Selected as the strongest place guess {source_label}. Generic/contextual places remain non-GPS by default.",
            confidence=_number_or_none((selected or {}).get("confidence")) or confidence,
            payload={
                "label": label,
                "scope_label": scope,
                "place_type": place_type,
                "selected_location_option_id": (selected or {}).get("id") or "selected",
                "selected_location_option": selected or {},
                "exportable_as_gps": exportable_as_gps,
            },
        )

    if task_type == "resolve_person":
        label = str(candidate.get("label") or "").strip()
        aliases = [str(alias) for alias in candidate.get("aliases") or [] if alias]
        if str(candidate.get("kind") or "") == "role_candidate":
            role_identity_options = candidate.get("role_identity_options") if isinstance(candidate.get("role_identity_options"), list) else []
            top_option = role_identity_options[0] if role_identity_options and isinstance(role_identity_options[0], dict) else {}
            merge_with = str(top_option.get("person_group_id") or "")
            if merge_with:
                return _suggestion(
                    action="merge_person",
                    target_id=str(item.get("source_id") or ""),
                    target_type=str(item.get("source_record_type") or "people_group"),
                    label=f"Merge {label or 'this role'} into {top_option.get('label')}",
                    rationale="A named relationship candidate and the role's event context point to the same person; keep reviewable before changing durable identity data.",
                    confidence=_number_or_none(top_option.get("confidence")) or confidence,
                    payload={
                        "merge_with_person_group_id": merge_with,
                        "role_identity_option": top_option,
                    },
                )
            return _suggestion(
                action="mark_role_only",
                target_id=str(item.get("source_id") or ""),
                target_type=str(item.get("source_record_type") or "people_group"),
                label=f"Keep {label or 'this mention'} as a role until stronger identity evidence appears",
                rationale="Role-only mentions are useful context but should not become durable person identities by default.",
                confidence=confidence,
                payload={},
            )
        return _suggestion(
            action="confirm_person",
            target_id=str(item.get("source_id") or ""),
            target_type=str(item.get("source_record_type") or "people_group"),
            label=f"Use person group: {label}",
            rationale="Alias and nickname variants are already grouped; this accepts the current best canonical person label.",
            confidence=confidence,
            payload={"label": label, "aliases": aliases},
        )

    if task_type == "review_event":
        if candidate.get("unverified_description"):
            # No safe automatic resolution exists for a description the
            # transcript contradicts — a human must watch the footage.
            return {}
        title = str(candidate.get("title") or item.get("title") or "").removeprefix("Review event: ").strip()
        relatedness = str(candidate.get("relatedness") or "")
        if "unrelated" in relatedness.casefold():
            return _suggestion(
                action="mark_unrelated",
                target_id=str(item.get("source_id") or ""),
                target_type=str(item.get("source_record_type") or "event"),
                label=f"Treat {title or 'this event'} as unrelated",
                rationale="The best current classification says this is probably not family footage.",
                confidence=confidence,
                payload={},
            )
        return _suggestion(
            action="confirm_event",
            target_id=str(item.get("source_id") or ""),
            target_type=str(item.get("source_record_type") or "event"),
            label=f"Keep event: {title}",
            rationale="Current title/type/relatedness are the best event-level guess; corrections can rename or mark unrelated.",
            confidence=confidence,
            payload={"title": title, "event_type": candidate.get("event_type"), "relatedness": relatedness},
        )

    if task_type == "resolve_date":
        date_value = candidate.get("date_value")
        precision = candidate.get("precision") or "unknown"
        label = str(candidate.get("label") or date_value or "date clue")
        historical = bool(candidate.get("excluded_as_event_date"))
        return _suggestion(
            action="mark_historical_context" if historical else "confirm_event_date",
            target_id=str(item.get("source_id") or ""),
            target_type=str(item.get("source_record_type") or "date_group"),
            label=f"{'Keep as context only' if historical else 'Use as event date'}: {label}",
            rationale="Date clues default to context when the pipeline marked them as historical/loose, otherwise to event-date metadata.",
            confidence=confidence,
            payload={"label": label, "date_value": date_value, "precision": precision},
        )

    return {}


def _suggestion(
    *,
    action: str,
    target_id: str,
    target_type: str,
    label: str,
    rationale: str,
    confidence: float | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": action,
        "target_id": target_id,
        "target_type": target_type,
        "label": label,
        "rationale": rationale,
        "confidence": confidence,
        "payload": {key: value for key, value in payload.items() if value is not None},
    }


def _media_record(tape: dict[str, Any], *, offset_s: float = 0.0) -> dict[str, Any]:
    probe = tape.get("probe") if isinstance(tape.get("probe"), dict) else {}
    return {
        "id": str(tape.get("id") or ""),
        "filename": tape.get("filename"),
        "relative_path": tape.get("relative_path"),
        "offset_s": offset_s,
        "duration_s": probe.get("duration_s"),
        "width": (probe.get("video") or {}).get("width") if isinstance(probe.get("video"), dict) else None,
        "height": (probe.get("video") or {}).get("height") if isinstance(probe.get("video"), dict) else None,
    }


def _video_offsets(tapes: list[dict[str, Any]]) -> dict[str, float]:
    offsets: dict[str, float] = {}
    cursor = 0.0
    for tape in tapes:
        tape_id = str(tape.get("id") or "")
        if tape_id:
            offsets[tape_id] = cursor
        probe = tape.get("probe") if isinstance(tape.get("probe"), dict) else {}
        duration = _number_or_none(probe.get("duration_s"))
        if duration is not None:
            cursor += duration
    return offsets


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
                    "quality_status": cluster.get("quality_status") or candidate.get("face_quality_status"),
                    "face_quality_notes": cluster.get("face_quality_notes") or candidate.get("face_quality_notes") or [],
                    "review_only": bool(cluster.get("review_only")),
                    "review_status": cluster.get("review_status") or "needs_review",
                }
            )
    return dict(grouped)


def _face_identity_candidates_by_cluster(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        cluster_id = str(candidate.get("face_cluster_id") or "")
        if cluster_id:
            grouped[cluster_id].append(candidate)
    return {
        cluster_id: sorted(rows, key=lambda row: (_number_or_none(row.get("confidence")) or 0.0), reverse=True)
        for cluster_id, rows in grouped.items()
    }


def _face_identity_candidate_for_review(
    candidate: dict[str, Any],
    people_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    person = people_by_id.get(str(candidate.get("person_group_id") or "")) or {}
    return {
        "face_identity_candidate_id": str(candidate.get("id") or ""),
        "person_group_id": str(candidate.get("person_group_id") or ""),
        "person_label": str(candidate.get("person_label") or person.get("label") or ""),
        "confidence": candidate.get("confidence"),
        "supporting_event_ids": [str(item) for item in candidate.get("supporting_event_ids") or []],
        "supporting_event_titles": [str(item) for item in candidate.get("supporting_event_titles") or []],
        "direct_name_event_ids": [str(item) for item in candidate.get("direct_name_event_ids") or []],
        "direct_name_strength": candidate.get("direct_name_strength"),
        "candidate_ambiguity": candidate.get("candidate_ambiguity"),
        "average_event_people_count": candidate.get("average_event_people_count"),
        "face_quality_status": candidate.get("face_quality_status"),
        "face_quality_notes": [str(item) for item in candidate.get("face_quality_notes") or []],
        "basis": [str(item) for item in candidate.get("basis") or []],
        "review_status": candidate.get("review_status") or "needs_review",
    }


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


def _event_thumbnail_path(event_ids: list[Any], assets_by_subject: dict[str, list[dict[str, Any]]]) -> str:
    for event_id in event_ids:
        path = _first_path(assets_by_subject.get(f"event:{event_id}", []), "thumbnail_path")
        if path:
            return path
    return ""


def _scope_event_ids(
    scope: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    video_offsets: dict[str, float],
) -> list[str]:
    explicit = [str(item) for item in scope.get("canonical_event_ids") or [] if item]
    if explicit:
        return _unique_items(explicit)

    source_video_ids = {str(item) for item in scope.get("source_video_ids") or [] if item}
    if not source_video_ids:
        return []

    start = _number_or_none(scope.get("start_s"))
    end = _number_or_none(scope.get("end_s"))
    if start is None and end is None:
        return []
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None:
        return []
    if end < start:
        start, end = end, start

    matched = _scope_event_ids_with_range(source_video_ids, start, end, events, video_offsets=video_offsets)
    if matched:
        return matched
    return _scope_event_ids_with_range(source_video_ids, start, end, events, video_offsets={})


def _scope_event_ids_with_range(
    source_video_ids: set[str],
    start: float,
    end: float,
    events: list[dict[str, Any]],
    *,
    video_offsets: dict[str, float],
) -> list[str]:
    event_ids: list[str] = []
    ranges = [
        (start + video_offsets.get(video_id, 0.0), end + video_offsets.get(video_id, 0.0))
        for video_id in source_video_ids
    ]
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        if source_video_ids and source_video_ids.isdisjoint(set(_event_source_video_ids(event))):
            continue
        event_start = _number_or_none(event.get("start_s"))
        event_end = _number_or_none(event.get("end_s"))
        if event_start is None and event_end is None:
            continue
        if event_start is None:
            event_start = event_end
        if event_end is None:
            event_end = event_start
        if event_start is None or event_end is None:
            continue
        if event_end < event_start:
            event_start, event_end = event_end, event_start
        if any(_ranges_overlap(start_s, end_s, event_start, event_end) for start_s, end_s in ranges):
            event_ids.append(event_id)
    return _unique_items(event_ids)


def _ranges_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> bool:
    return start_a <= end_b and start_b <= end_a


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


def _place_evidence_basis(place: dict[str, Any]) -> dict[str, Any]:
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    source_label = str(metadata.get("inference_source_label") or "").strip()
    role_counts = metadata.get("location_role_counts") if isinstance(metadata.get("location_role_counts"), dict) else {}
    basis = _unique_items(
        [
            *[str(label) for label in metadata.get("inference_source_labels") or [] if label],
            *[str(role) for role in role_counts if role],
        ]
    )
    return {
        "source_label": _source_label_for_display(source_label),
        "raw_source_label": source_label,
        "role_counts": role_counts,
        "basis": basis,
        "evidence_texts": [str(item) for item in metadata.get("source_evidence_texts") or [] if item][:4],
        "event_place_role_ids": [str(item) for item in metadata.get("event_place_role_ids") or [] if item],
        "summary": _place_basis_summary(place, source_label, role_counts),
    }


def _place_basis_summary(place: dict[str, Any], source_label: str, role_counts: dict[str, Any]) -> str:
    label = str(place.get("label") or "this place")
    if "generic_scene_type" in role_counts:
        return f"{label} appears to be a scene/place type from visual context."
    if "administrative_context" in role_counts:
        return f"{label} appears to be broader location context."
    if source_label:
        return f"{label} appears to be inferred {_source_label_for_display(source_label)}."
    return f"{label} appears to be a place candidate."


def _place_location_options(place: dict[str, Any]) -> list[dict[str, Any]]:
    options = []
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    selected_basis = _place_option_basis(place)
    selected_source_label = _selected_place_option_source_label(place, selected_basis)
    selected = {
        "id": "selected",
        "label": str(place.get("label") or ""),
        "display_label": _place_display_label(place),
        "scope_label": place.get("scope_label") or "",
        "place_type": place.get("place_type") or "",
        "source_label": selected_source_label,
        "confidence": place.get("confidence"),
        "selected": True,
        "basis": selected_basis,
    }
    options.append(selected)

    for group_key, label_prefix in [
        ("parent_place_candidates", "from context"),
        ("nearby_place_candidates", "nearby context"),
    ]:
        for candidate in metadata.get(group_key) or []:
            if not isinstance(candidate, dict):
                continue
            candidate_label = str(candidate.get("label") or "").strip()
            if not candidate_label:
                continue
            display_label = f"{place.get('label')} ({candidate_label} context)"
            basis_values = [str(item) for item in candidate.get("basis") or [] if item]
            option = {
                "id": f"{group_key}:{candidate.get('place_group_id') or candidate_label}:{candidate.get('relation') or ''}",
                "label": str(place.get("label") or ""),
                "display_label": display_label,
                "scope_label": f"{candidate_label} context",
                "place_type": place.get("place_type") or "",
                "source_label": _place_candidate_source_label(basis_values, label_prefix),
                "confidence": candidate.get("confidence"),
                "selected": False,
                "basis": basis_values,
                "target_place_group_id": candidate.get("place_group_id"),
                "relation": candidate.get("relation"),
            }
            if not any(existing["display_label"] == option["display_label"] for existing in options):
                options.append(option)

    return options[:6]


def _selected_place_option_source_label(place: dict[str, Any], basis: list[str]) -> str:
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    scope_label = str(place.get("scope_label") or "")
    for candidate in metadata.get("parent_place_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        candidate_label = str(candidate.get("label") or "")
        if candidate_label and candidate_label in scope_label:
            candidate_basis = [str(item) for item in candidate.get("basis") or [] if item]
            basis.extend(item for item in candidate_basis if item not in basis)
            return _place_candidate_source_label(candidate_basis, "from context")
    return _source_label_for_display(_place_source_label(place))


def _place_source_label(place: dict[str, Any]) -> str:
    metadata = place.get("metadata") if isinstance(place.get("metadata"), dict) else {}
    return str(metadata.get("inference_source_label") or "")


def _place_candidate_source_label(basis: list[str], fallback: str) -> str:
    if any("continuity" in item for item in basis):
        return "from continuity"
    if any("direct" in item for item in basis):
        return "from direct context"
    if any("nearby" in item for item in basis):
        return "nearby context"
    return fallback


def _source_label_for_display(value: str) -> str:
    labels = {
        "administrative context": "from broader location context",
        "direct location mention": "from direct mention",
        "visual/model place clue": "from visual/model context",
        "visual scene type": "from visual context",
    }
    return labels.get(str(value or ""), str(value or "from context"))


def _place_option_basis(place: dict[str, Any]) -> list[str]:
    basis = []
    evidence_basis = _place_evidence_basis(place)
    if evidence_basis.get("source_label"):
        basis.append(str(evidence_basis["source_label"]))
    for note in place.get("notes") or []:
        if note:
            basis.append(str(note))
    return _unique_items(basis)


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


def _description_unverified(alignment: dict[str, Any] | None) -> bool:
    """True when the event's description lacks transcript support.

    Video models occasionally confabulate a description for a stretch of
    footage (a 'lab tour' over ski footage). The alignment stage already
    scores each event against the tape-absolute transcript; a weakly aligned
    event whose nearby-transcript support is near zero should be verified by
    a human and must never be auto-accepted.
    """

    if not isinstance(alignment, dict):
        return False
    timing = str(alignment.get("timing_status") or "")
    if timing not in {"weakly_aligned", "unaligned", "unsupported"}:
        return False
    support = _number_or_none(alignment.get("support_score"))
    if support is not None and support > 0.45:
        return False
    warnings = " ".join(str(w) for w in alignment.get("warnings") or [])
    return "transcript" in warnings.casefold()


def _needs_review(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").casefold() in {"needs_review", "open"}


def _review_closed(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").casefold() in {"confirmed", "rejected", "excluded"}


def _relationship_review_groups(relationships: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for relationship in relationships:
        if not _needs_review(relationship):
            continue
        key = (
            _relationship_party_key(relationship, "subject"),
            _normalize_context_text(relationship.get("predicate")),
            _relationship_party_key(relationship, "object"),
        )
        buckets.setdefault(key, []).append(relationship)
    return sorted(
        (
            sorted(
                group,
                key=lambda row: (
                    -(_number_or_none(row.get("confidence")) or 0.0),
                    str(row.get("id") or ""),
                ),
            )
            for group in buckets.values()
        ),
        key=lambda group: (
            -(_number_or_none(group[0].get("confidence")) or 0.0),
            str(group[0].get("subject_label") or "").casefold(),
            str(group[0].get("object_label") or "").casefold(),
        ),
    )


def _relationship_party_key(relationship: dict[str, Any], role: str) -> str:
    entity_id = str(relationship.get(f"{role}_entity_id") or "").strip()
    if entity_id:
        return f"id:{entity_id}"
    return f"label:{_normalize_context_text(relationship.get(f'{role}_label'))}"


def _role_identity_options_by_person(
    people: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    people_by_id = {str(person.get("id") or ""): person for person in people if person.get("id")}
    person_id_by_alias = _person_id_by_alias(people)
    named_relationships: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for relationship in relationships:
        predicate = str(relationship.get("predicate") or "")
        subject_id = str(relationship.get("subject_entity_id") or "")
        object_id = str(relationship.get("object_entity_id") or "")
        subject = people_by_id.get(subject_id)
        if not subject or str(subject.get("kind") or "") == "role_candidate" or not object_id:
            continue
        named_relationships[(predicate, object_id)].append(relationship)

    options_by_person: dict[str, list[dict[str, Any]]] = {}
    for person in people:
        person_id = str(person.get("id") or "")
        predicate = _role_person_predicate(person)
        if not person_id or not predicate:
            continue
        options = []
        for object_id in _role_event_object_person_ids(person, events_by_id, person_id_by_alias, exclude_id=person_id):
            for relationship in named_relationships.get((predicate, object_id), []):
                subject_id = str(relationship.get("subject_entity_id") or "")
                subject = people_by_id.get(subject_id)
                if not subject:
                    continue
                confidence = min(0.95, (_number_or_none(relationship.get("confidence")) or 0.7) + 0.04)
                options.append(
                    {
                        "person_group_id": subject_id,
                        "label": subject.get("label"),
                        "confidence": round(confidence, 3),
                        "predicate": predicate,
                        "object_person_group_id": object_id,
                        "object_label": (people_by_id.get(object_id) or {}).get("label") or relationship.get("object_label"),
                        "relationship_candidate_id": relationship.get("id"),
                        "basis": [
                            f"{relationship.get('subject_label')} is a {predicate.replace('_candidate', '').replace('_', ' ')} of {relationship.get('object_label')}",
                            f"{person.get('label')} appears as a role in event(s) with {(people_by_id.get(object_id) or {}).get('label') or object_id}",
                        ],
                    }
                )
        if options:
            options_by_person[person_id] = _dedupe_role_identity_options(options)
    return options_by_person


def _person_id_by_alias(people: list[dict[str, Any]]) -> dict[str, str]:
    lookup = {}
    for person in people:
        person_id = str(person.get("id") or "")
        labels = [person.get("label"), *(person.get("aliases") or [])]
        for label in labels:
            key = _normalize_context_text(label)
            if key and person_id:
                lookup[key] = person_id
            for part in str(label or "").replace("/", " ").replace("(", " ").replace(")", " ").split():
                part_key = _normalize_context_text(part)
                if part_key and person_id:
                    lookup.setdefault(part_key, person_id)
    return lookup


def _role_person_predicate(person: dict[str, Any]) -> str:
    if str(person.get("kind") or "") != "role_candidate":
        return ""
    role_text = " ".join([str(person.get("label") or ""), *[str(alias) for alias in person.get("aliases") or []]]).casefold()
    if any(term in role_text for term in ["mom", "mother", "mama", "мама", "маму", "маме"]):
        return "mother_candidate"
    if any(term in role_text for term in ["dad", "father", "papa", "папа", "папу", "папе"]):
        return "father_candidate"
    return ""


def _role_event_object_person_ids(
    person: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    person_id_by_alias: dict[str, str],
    *,
    exclude_id: str,
) -> list[str]:
    object_ids = []
    for event_id in person.get("canonical_event_ids") or []:
        event = events_by_id.get(str(event_id)) or {}
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        for label in metadata.get("people") or []:
            object_id = person_id_by_alias.get(_normalize_context_text(label))
            if object_id and object_id != exclude_id:
                object_ids.append(object_id)
    return _unique_items(object_ids)


def _dedupe_role_identity_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for option in options:
        key = str(option.get("person_group_id") or "")
        current = buckets.get(key)
        if current is None or (_number_or_none(option.get("confidence")) or 0.0) > (_number_or_none(current.get("confidence")) or 0.0):
            buckets[key] = option
    return sorted(buckets.values(), key=lambda row: -(_number_or_none(row.get("confidence")) or 0.0))


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


def _humanize_token(value: Any) -> str:
    return " ".join(str(value or "").replace("_", " ").split())


def _first_path(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _representative_path(rows: list[dict[str, Any]], key: str) -> str:
    """Pick the temporally middle frame, not the lead-in.

    The first keyframe of an event is usually its establishing shot (or the
    tail of the previous recording); the middle of the event is far more
    likely to show what the title describes.
    """

    candidates = [row for row in rows if row.get(key)]
    if not candidates:
        return ""
    candidates.sort(key=lambda row: float(row.get("time_s") or 0.0))
    return str(candidates[len(candidates) // 2].get(key) or "")


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

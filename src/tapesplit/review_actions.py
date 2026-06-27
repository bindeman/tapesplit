from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


SUPPORTED_REVIEW_ACTIONS = {
    "confirm_identity",
    "reject_identity",
    "confirm_person",
    "merge_person",
    "mark_role_only",
    "rename_person",
    "confirm_place",
    "split_place",
    "rename_place",
    "mark_not_location",
    "confirm_place_context",
    "confirm_geocode_later",
    "reject_place_context",
    "confirm_relationship",
    "reject_relationship",
    "edit_relationship",
    "confirm_event",
    "rename_event",
    "split_event",
    "mark_unrelated",
    "confirm_event_date",
    "mark_historical_context",
    "edit_date",
}


def apply_review_actions(
    project_dir: Path,
    *,
    actions_path: Path | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    pending_actions = _load_actions(actions_path) if actions_path else list(actions or [])
    if not pending_actions:
        raise ValueError("no review actions provided")

    state = _ProjectReviewState(project)
    corrections = read_jsonl(project / "corrections.jsonl")
    next_index = len(corrections) + 1
    normalized_actions = []
    touched_files: set[str] = set()

    for offset, action in enumerate(pending_actions):
        correction = _normalize_action(action, correction_index=next_index + offset)
        effects = _apply_action(state, correction)
        correction["applied_effects"] = effects
        corrections.append(correction)
        normalized_actions.append(correction)
        touched_files.update(effect["file"] for effect in effects if effect.get("file"))

    state.write(touched_files)
    _write_jsonl(project / "corrections.jsonl", corrections)
    return {
        "project": str(project),
        "actions_applied": len(normalized_actions),
        "by_action": _count_by(normalized_actions, "action"),
        "touched_files": sorted(touched_files | {"corrections.jsonl"}),
        "outputs": {"corrections": str(project / "corrections.jsonl")},
    }


def reapply_review_corrections(
    project_dir: Path,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    corrections = read_jsonl(project / "corrections.jsonl")
    if not corrections:
        return {
            "project": str(project),
            "corrections": 0,
            "corrections_applied": 0,
            "corrections_skipped": 0,
            "by_action": {},
            "touched_files": [],
            "skipped": [],
        }

    state = _ProjectReviewState(project)
    touched_files: set[str] = set()
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for correction in corrections:
        try:
            effects = _apply_action(state, correction)
        except ValueError as exc:
            if strict or not _is_missing_target_error(exc):
                raise
            skipped.append(
                {
                    "id": correction.get("id"),
                    "action": correction.get("action"),
                    "target_id": correction.get("target_id"),
                    "reason": str(exc),
                }
            )
            continue
        applied.append(correction)
        touched_files.update(effect["file"] for effect in effects if effect.get("file"))

    state.write(touched_files)
    return {
        "project": str(project),
        "corrections": len(corrections),
        "corrections_applied": len(applied),
        "corrections_skipped": len(skipped),
        "by_action": _count_by(applied, "action"),
        "touched_files": sorted(touched_files),
        "skipped": skipped,
    }


def list_review_corrections(project_dir: Path) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    corrections = read_jsonl(project / "corrections.jsonl")
    return {
        "project": str(project),
        "corrections": len(corrections),
        "by_action": _count_by(corrections, "action"),
        "items": corrections,
    }


class _ProjectReviewState:
    def __init__(self, project: Path):
        self.project = project
        self.rows_by_file = {
            "canonical_events.jsonl": read_jsonl(project / "canonical_events.jsonl"),
            "context_edges.jsonl": read_jsonl(project / "context_edges.jsonl"),
            "date_groups.jsonl": read_jsonl(project / "date_groups.jsonl"),
            "edge_metrics.jsonl": read_jsonl(project / "edge_metrics.jsonl"),
            "face_clusters.jsonl": read_jsonl(project / "face_clusters.jsonl"),
            "face_identity_candidates.jsonl": read_jsonl(project / "face_identity_candidates.jsonl"),
            "face_observations.jsonl": read_jsonl(project / "face_observations.jsonl"),
            "people_groups.jsonl": read_jsonl(project / "people_groups.jsonl"),
            "place_groups.jsonl": read_jsonl(project / "place_groups.jsonl"),
            "relationship_candidates.jsonl": read_jsonl(project / "relationship_candidates.jsonl"),
        }

    def rows(self, filename: str) -> list[dict[str, Any]]:
        return self.rows_by_file[filename]

    def row_by_id(self, filename: str, row_id: str) -> dict[str, Any] | None:
        return next((row for row in self.rows(filename) if str(row.get("id") or "") == row_id), None)

    def write(self, filenames: set[str]) -> None:
        for filename in sorted(filenames):
            if filename not in self.rows_by_file:
                continue
            _write_jsonl(self.project / filename, self.rows_by_file[filename])


def _apply_action(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    action = correction["action"]
    if action not in SUPPORTED_REVIEW_ACTIONS:
        raise ValueError(f"unsupported review action: {action}")
    dispatch = {
        "confirm_identity": _confirm_identity,
        "reject_identity": _reject_identity,
        "confirm_person": _confirm_person,
        "merge_person": _merge_person,
        "mark_role_only": _mark_role_only,
        "rename_person": _rename_person,
        "confirm_place": _confirm_place,
        "split_place": _request_manual_split,
        "rename_place": _rename_place,
        "mark_not_location": _mark_not_location,
        "confirm_place_context": _confirm_place_context,
        "confirm_geocode_later": _record_followup_action,
        "reject_place_context": _reject_place_context,
        "confirm_relationship": _confirm_relationship,
        "reject_relationship": _reject_relationship,
        "edit_relationship": _edit_relationship,
        "confirm_event": _confirm_event,
        "rename_event": _rename_event,
        "split_event": _request_manual_split,
        "mark_unrelated": _mark_unrelated,
        "confirm_event_date": _confirm_event_date,
        "mark_historical_context": _mark_historical_context,
        "edit_date": _edit_date,
    }
    return dispatch[action](state, correction)


def _confirm_identity(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _require_target(state, "face_identity_candidates.jsonl", correction)
    payload = correction["payload"]
    person_group_id = str(payload.get("person_group_id") or candidate.get("person_group_id") or "")
    face_cluster_id = str(payload.get("face_cluster_id") or candidate.get("face_cluster_id") or "")
    if not person_group_id or not face_cluster_id:
        raise ValueError("confirm_identity requires person_group_id and face_cluster_id")

    effects = []
    _mark_reviewed(candidate, "confirmed", correction)
    candidate["person_group_id"] = person_group_id
    candidate["face_cluster_id"] = face_cluster_id
    effects.append(_effect("face_identity_candidates.jsonl", candidate, "confirmed identity candidate"))

    cluster = state.row_by_id("face_clusters.jsonl", face_cluster_id)
    if cluster:
        _mark_reviewed(cluster, "confirmed", correction)
        cluster["linked_person_group_id"] = person_group_id
        for row in cluster.get("candidate_people") or []:
            if str(row.get("person_group_id") or "") == person_group_id:
                row["review_status"] = "confirmed"
        effects.append(_effect("face_clusters.jsonl", cluster, "linked face cluster to person"))

    for face in state.rows("face_observations.jsonl"):
        if str(face.get("face_cluster_id") or "") != face_cluster_id:
            continue
        face["person_group_id"] = person_group_id
        face["identity_review_status"] = "confirmed"
        _append_unique(face, "review_correction_ids", correction["id"])
        effects.append(_effect("face_observations.jsonl", face, "linked face observation to person"))

    person = state.row_by_id("people_groups.jsonl", person_group_id)
    if person:
        _append_unique(person, "confirmed_face_cluster_ids", face_cluster_id)
        _append_unique(person, "review_correction_ids", correction["id"])
        effects.append(_effect("people_groups.jsonl", person, "stored confirmed face cluster on person"))
    return effects


def _reject_identity(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _require_target(state, "face_identity_candidates.jsonl", correction)
    _mark_reviewed(candidate, "rejected", correction)
    face_cluster_id = str(candidate.get("face_cluster_id") or correction["payload"].get("face_cluster_id") or "")
    person_group_id = str(candidate.get("person_group_id") or correction["payload"].get("person_group_id") or "")
    cluster = state.row_by_id("face_clusters.jsonl", face_cluster_id)
    effects = [_effect("face_identity_candidates.jsonl", candidate, "rejected identity candidate")]
    if cluster:
        for row in cluster.get("candidate_people") or []:
            if str(row.get("person_group_id") or "") == person_group_id:
                row["review_status"] = "rejected"
        _append_unique(cluster, "review_correction_ids", correction["id"])
        effects.append(_effect("face_clusters.jsonl", cluster, "rejected person candidate on face cluster"))
    return effects


def _confirm_person(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    person = _require_target(state, "people_groups.jsonl", correction)
    _apply_label_payload(person, correction["payload"])
    _mark_reviewed(person, "confirmed", correction)
    return [_effect("people_groups.jsonl", person, "confirmed person")]


def _merge_person(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    source = _require_target(state, "people_groups.jsonl", correction)
    payload = correction["payload"]
    destination_id = str(payload.get("merge_with_person_group_id") or payload.get("destination_person_group_id") or "")
    destination = state.row_by_id("people_groups.jsonl", destination_id)
    if not destination:
        raise ValueError("merge_person requires merge_with_person_group_id or destination_person_group_id")
    source_id = str(source.get("id") or "")
    effects = []

    for value in [source.get("label"), *list(source.get("aliases") or [])]:
        _append_unique(destination, "aliases", str(value or ""))
    for key in ["canonical_event_ids", "evidence_ids", "source_video_ids", "confirmed_face_cluster_ids"]:
        for value in source.get(key) or []:
            _append_unique(destination, key, value)
    _append_unique(destination, "merged_person_group_ids", source_id)
    _mark_reviewed(destination, "confirmed", correction)
    effects.append(_effect("people_groups.jsonl", destination, "merged person into destination"))

    _metadata(source)["merged_into_person_group_id"] = destination_id
    source["merged_into_person_group_id"] = destination_id
    _mark_reviewed(source, "merged", correction)
    effects.append(_effect("people_groups.jsonl", source, "marked source person as merged"))

    for face in state.rows("face_observations.jsonl"):
        if str(face.get("person_group_id") or "") != source_id:
            continue
        face["person_group_id"] = destination_id
        _append_unique(face, "review_correction_ids", correction["id"])
        effects.append(_effect("face_observations.jsonl", face, "moved face observation to merged person"))

    for cluster in state.rows("face_clusters.jsonl"):
        cluster_changed = False
        if str(cluster.get("linked_person_group_id") or "") == source_id:
            cluster["linked_person_group_id"] = destination_id
            _append_unique(cluster, "review_correction_ids", correction["id"])
            cluster_changed = True
        for candidate in cluster.get("candidate_people") or []:
            if str(candidate.get("person_group_id") or "") == source_id:
                candidate["person_group_id"] = destination_id
                candidate["person_label"] = str(destination.get("label") or destination_id)
                cluster_changed = True
        if cluster_changed:
            _append_unique(cluster, "review_correction_ids", correction["id"])
            effects.append(_effect("face_clusters.jsonl", cluster, "moved face cluster to merged person"))

    for candidate in state.rows("face_identity_candidates.jsonl"):
        if str(candidate.get("person_group_id") or "") != source_id:
            continue
        candidate["person_group_id"] = destination_id
        candidate["person_label"] = str(destination.get("label") or destination_id)
        _append_unique(candidate, "review_correction_ids", correction["id"])
        effects.append(_effect("face_identity_candidates.jsonl", candidate, "moved identity candidate to merged person"))
    return effects


def _mark_role_only(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    person = _require_target(state, "people_groups.jsonl", correction)
    person["kind"] = "role_candidate"
    _metadata(person)["identity_role_only"] = True
    _mark_reviewed(person, "confirmed", correction)
    return [_effect("people_groups.jsonl", person, "marked person candidate as role-only")]


def _rename_person(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    person = _require_target(state, "people_groups.jsonl", correction)
    _apply_label_payload(person, correction["payload"])
    _mark_reviewed(person, correction["payload"].get("review_status") or "needs_review", correction)
    return [_effect("people_groups.jsonl", person, "renamed person")]


def _confirm_place(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    place = _require_target(state, "place_groups.jsonl", correction)
    _apply_place_payload(place, correction["payload"])
    _mark_reviewed(place, "confirmed", correction)
    return [_effect("place_groups.jsonl", place, "confirmed place")]


def _rename_place(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    place = _require_target(state, "place_groups.jsonl", correction)
    _apply_place_payload(place, correction["payload"])
    _mark_reviewed(place, correction["payload"].get("review_status") or "needs_review", correction)
    return [_effect("place_groups.jsonl", place, "renamed/scoped place")]


def _mark_not_location(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    place = _require_target(state, "place_groups.jsonl", correction)
    metadata = _metadata(place)
    metadata["location_role"] = "mentioned_not_filming_location"
    place["not_exportable_as_gps"] = True
    _mark_reviewed(place, "rejected", correction)
    return [_effect("place_groups.jsonl", place, "marked place as non-location clue")]


def _confirm_place_context(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    edge = _require_target(state, "context_edges.jsonl", correction)
    payload = correction["payload"]
    metadata = _metadata(edge)
    if "exportable_as_gps" in payload:
        metadata["not_exportable_as_gps"] = not _truthy(payload.get("exportable_as_gps"))
    _mark_reviewed(edge, "confirmed", correction)
    effects = [_effect("context_edges.jsonl", edge, "confirmed place context edge")]
    effects.extend(_update_place_context_candidate(state, edge, correction, "confirmed"))
    effects.extend(_sync_edge_metric_status(state, edge, correction, "confirmed"))
    return effects


def _reject_place_context(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    edge = _require_target(state, "context_edges.jsonl", correction)
    _metadata(edge)["not_exportable_as_gps"] = True
    _mark_reviewed(edge, "rejected", correction)
    effects = [_effect("context_edges.jsonl", edge, "rejected place context edge")]
    effects.extend(_update_place_context_candidate(state, edge, correction, "rejected"))
    effects.extend(_sync_edge_metric_status(state, edge, correction, "rejected"))
    return effects


def _confirm_relationship(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    relationship = _require_target(state, "relationship_candidates.jsonl", correction)
    _apply_relationship_payload(relationship, correction["payload"])
    _mark_reviewed(relationship, "confirmed", correction)
    return [_effect("relationship_candidates.jsonl", relationship, "confirmed relationship")]


def _reject_relationship(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    relationship = _require_target(state, "relationship_candidates.jsonl", correction)
    _mark_reviewed(relationship, "rejected", correction)
    return [_effect("relationship_candidates.jsonl", relationship, "rejected relationship")]


def _edit_relationship(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    relationship = _require_target(state, "relationship_candidates.jsonl", correction)
    _apply_relationship_payload(relationship, correction["payload"])
    _mark_reviewed(relationship, correction["payload"].get("review_status") or "needs_review", correction)
    return [_effect("relationship_candidates.jsonl", relationship, "edited relationship")]


def _confirm_event(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    event = _require_target(state, "canonical_events.jsonl", correction)
    _apply_event_payload(event, correction["payload"])
    _mark_reviewed(event, "confirmed", correction)
    return [_effect("canonical_events.jsonl", event, "confirmed event")]


def _rename_event(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    event = _require_target(state, "canonical_events.jsonl", correction)
    _apply_event_payload(event, correction["payload"])
    _mark_reviewed(event, correction["payload"].get("review_status") or "needs_review", correction)
    return [_effect("canonical_events.jsonl", event, "renamed event")]


def _mark_unrelated(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    event = _require_target(state, "canonical_events.jsonl", correction)
    event["relatedness"] = "likely_unrelated"
    _metadata(event)["relatedness"] = "likely_unrelated"
    _mark_reviewed(event, "excluded", correction)
    return [_effect("canonical_events.jsonl", event, "marked event unrelated/excluded")]


def _confirm_event_date(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    date = _require_target(state, "date_groups.jsonl", correction)
    _apply_date_payload(date, correction["payload"])
    date["excluded_as_event_date"] = False
    _mark_reviewed(date, "confirmed", correction)
    return [_effect("date_groups.jsonl", date, "confirmed event date")]


def _mark_historical_context(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    date = _require_target(state, "date_groups.jsonl", correction)
    _apply_date_payload(date, correction["payload"])
    date["excluded_as_event_date"] = True
    _mark_reviewed(date, "confirmed", correction)
    return [_effect("date_groups.jsonl", date, "marked date as historical context")]


def _edit_date(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    date = _require_target(state, "date_groups.jsonl", correction)
    _apply_date_payload(date, correction["payload"])
    _mark_reviewed(date, correction["payload"].get("review_status") or "needs_review", correction)
    return [_effect("date_groups.jsonl", date, "edited date")]


def _request_manual_split(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    filename = _target_filename(correction)
    if not filename:
        return [_effect("", {"id": correction["target_id"]}, "recorded manual split request")]
    target = _require_target(state, filename, correction)
    _metadata(target)["manual_split_requested"] = True
    _mark_reviewed(target, "needs_review", correction)
    return [_effect(filename, target, "recorded manual split request")]


def _record_followup_action(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    filename = _target_filename(correction)
    if not filename:
        return [_effect("", {"id": correction["target_id"]}, "recorded review follow-up")]
    target = _require_target(state, filename, correction)
    _append_unique(target, "review_correction_ids", correction["id"])
    metadata = _metadata(target)
    followups = metadata.get("review_followups")
    if not isinstance(followups, list):
        followups = []
        metadata["review_followups"] = followups
    followups.append(
        {
            "correction_id": correction["id"],
            "action": correction["action"],
            "reviewed_at": correction["reviewed_at"],
            "payload": correction["payload"],
        }
    )
    return [_effect(filename, target, "recorded review follow-up")]


def _apply_label_payload(row: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("label"):
        old_label = str(row.get("label") or "")
        new_label = str(payload["label"])
        if old_label and old_label != new_label:
            _append_unique(row, "aliases", old_label)
        row["label"] = new_label
    if isinstance(payload.get("aliases"), list):
        for alias in payload["aliases"]:
            _append_unique(row, "aliases", str(alias))


def _apply_place_payload(place: dict[str, Any], payload: dict[str, Any]) -> None:
    _apply_label_payload(place, payload)
    for key in ["place_type", "scope_label"]:
        if payload.get(key) not in (None, ""):
            place[key] = payload[key]
    for key in ["parent_place_labels", "nearby_place_labels"]:
        if isinstance(payload.get(key), list):
            place[key] = [str(item) for item in payload[key] if item]

    metadata = _metadata(place)
    reviewed_scope = metadata.setdefault("reviewed_scope", {})
    for key in ["start_year", "end_year", "start_date", "end_date", "label"]:
        if payload.get(key) not in (None, ""):
            reviewed_scope[key] = payload[key]
    if reviewed_scope:
        metadata["reviewed_scope"] = reviewed_scope

    selected_geocode = _selected_geocode_from_payload(payload)
    if selected_geocode:
        metadata["selected_geocode"] = selected_geocode
        place["not_exportable_as_gps"] = not _truthy(payload.get("exportable_as_gps", True))
    elif "exportable_as_gps" in payload:
        place["not_exportable_as_gps"] = not _truthy(payload.get("exportable_as_gps"))
    else:
        place.setdefault("not_exportable_as_gps", True)


def _apply_relationship_payload(row: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in ["predicate", "subject_label", "object_label", "subject_entity_id", "object_entity_id"]:
        if payload.get(key) not in (None, ""):
            row[key] = payload[key]


def _apply_event_payload(row: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in ["title", "summary", "relatedness"]:
        if payload.get(key) not in (None, ""):
            row[key] = payload[key]
    metadata = _metadata(row)
    for key in ["event_type", "relatedness"]:
        if payload.get(key) not in (None, ""):
            metadata[key] = payload[key]


def _apply_date_payload(row: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in ["label", "date_value", "precision"]:
        if payload.get(key) not in (None, ""):
            row[key] = payload[key]


def _update_place_context_candidate(
    state: _ProjectReviewState,
    edge: dict[str, Any],
    correction: dict[str, Any],
    status: str,
) -> list[dict[str, Any]]:
    source_id = str(edge.get("subject_entity_id") or "")
    target_id = str(edge.get("object_entity_id") or "")
    predicate = str(edge.get("predicate") or "")
    place = state.row_by_id("place_groups.jsonl", source_id)
    if not place:
        return []
    changed = False
    metadata = _metadata(place)
    for list_key in ["parent_place_candidates", "nearby_place_candidates"]:
        for candidate in metadata.get(list_key) or []:
            if str(candidate.get("place_group_id") or "") != target_id:
                continue
            if str(candidate.get("relation") or "") != predicate:
                continue
            candidate["review_status"] = status
            if status == "confirmed" and "exportable_as_gps" in correction["payload"]:
                candidate["not_exportable_as_gps"] = not _truthy(correction["payload"].get("exportable_as_gps"))
            _append_unique(candidate, "review_correction_ids", correction["id"])
            changed = True
    if changed:
        _append_unique(place, "review_correction_ids", correction["id"])
        return [_effect("place_groups.jsonl", place, f"{status} place context candidate")]
    return []


def _sync_edge_metric_status(
    state: _ProjectReviewState,
    edge: dict[str, Any],
    correction: dict[str, Any],
    status: str,
) -> list[dict[str, Any]]:
    edge_id = str(edge.get("id") or "")
    effects = []
    for metric in state.rows("edge_metrics.jsonl"):
        if str(metric.get("edge_id") or "") != edge_id:
            continue
        _mark_reviewed(metric, status, correction)
        effects.append(_effect("edge_metrics.jsonl", metric, f"{status} edge metric"))
    return effects


def _require_target(state: _ProjectReviewState, filename: str, correction: dict[str, Any]) -> dict[str, Any]:
    target_id = str(correction.get("target_id") or "")
    row = state.row_by_id(filename, target_id)
    if not row:
        raise ValueError(f"{target_id} not found in {filename}")
    return row


def _is_missing_target_error(exc: ValueError) -> bool:
    return " not found in " in str(exc)


def _target_filename(correction: dict[str, Any]) -> str:
    target_type = str(correction.get("target_type") or _infer_target_type(str(correction.get("target_id") or "")))
    return {
        "context_edge": "context_edges.jsonl",
        "date_group": "date_groups.jsonl",
        "event": "canonical_events.jsonl",
        "face_identity_candidate": "face_identity_candidates.jsonl",
        "people_group": "people_groups.jsonl",
        "place_group": "place_groups.jsonl",
        "relationship_candidate": "relationship_candidates.jsonl",
    }.get(target_type, "")


def _mark_reviewed(row: dict[str, Any], status: str, correction: dict[str, Any]) -> None:
    row["review_status"] = status
    row["reviewed_at"] = correction["reviewed_at"]
    if correction.get("reviewer"):
        row["reviewed_by"] = correction["reviewer"]
    if correction.get("notes"):
        _append_unique(row, "notes", str(correction["notes"]))
    _append_unique(row, "review_correction_ids", correction["id"])


def _normalize_action(action: dict[str, Any], *, correction_index: int) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ValueError("review action must be a JSON object")
    action_name = str(action.get("action") or action.get("type") or "").strip()
    target_id = str(action.get("target_id") or action.get("source_id") or "").strip()
    if not action_name:
        raise ValueError("review action is missing action")
    if not target_id:
        raise ValueError("review action is missing target_id")
    payload = dict(action.get("payload") if isinstance(action.get("payload"), dict) else {})
    for key, value in action.items():
        if key in {"id", "action", "type", "target_id", "target_type", "source_id", "payload", "reviewer", "reviewed_at", "notes"}:
            continue
        payload.setdefault(key, value)
    return {
        "id": f"correction_{correction_index:06d}",
        "action": action_name,
        "target_type": str(action.get("target_type") or _infer_target_type(target_id)),
        "target_id": target_id,
        "reviewer": str(action.get("reviewer") or ""),
        "reviewed_at": str(action.get("reviewed_at") or _now_iso()),
        "notes": str(action.get("notes") or ""),
        "source_action_id": str(action.get("id") or ""),
        "payload": payload,
    }


def _infer_target_type(target_id: str) -> str:
    prefixes = {
        "canonical_event_": "event",
        "context_edge_": "context_edge",
        "date_group_": "date_group",
        "face_identity_candidate_": "face_identity_candidate",
        "people_group_": "people_group",
        "place_group_": "place_group",
        "relationship_candidate_": "relationship_candidate",
    }
    return next((value for prefix, value in prefixes.items() if target_id.startswith(prefix)), "")


def _load_actions(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    source = path.expanduser().resolve()
    raw = source.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if source.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    payload = json.loads(raw)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
        return payload["actions"]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"unsupported review action payload in {source}")


def _selected_geocode_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("selected_geocode"), dict):
        return dict(payload["selected_geocode"])
    lat = _number_or_none(payload.get("lat") or payload.get("latitude"))
    lng = _number_or_none(payload.get("lng") or payload.get("longitude"))
    if lat is None or lng is None:
        return {}
    result = {"lat": lat, "lng": lng}
    for key in ["formatted_address", "provider_place_id", "name", "provider"]:
        if payload.get(key):
            result[key] = payload[key]
    return result


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        row["metadata"] = metadata
    return metadata


def _append_unique(row: dict[str, Any], key: str, value: Any) -> None:
    if value in (None, ""):
        return
    values = row.get(key)
    if not isinstance(values, list):
        values = [values] if values not in (None, "") else []
        row[key] = values
    if value not in values:
        values.append(value)


def _effect(filename: str, row: dict[str, Any], message: str) -> dict[str, Any]:
    return {"file": filename, "id": str(row.get("id") or ""), "effect": message}


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
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

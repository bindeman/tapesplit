from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from tapesplit.claim_store import DualWriter
from tapesplit.face_embeddings import face_anchor
from tapesplit.storage import append_jsonl, read_jsonl


SUPPORTED_REVIEW_ACTIONS = {
    "confirm_identity",
    "label_face_cluster",
    "reject_identity",
    "detach_faces_from_cluster",
    "reassign_face_observations",
    "mark_face_unknown",
    "confirm_speaker_identity",
    "reject_speaker_identity",
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
    "move_event_range",
    "rename_event",
    "split_event",
    "mark_unrelated",
    "confirm_event_date",
    "mark_historical_context",
    "edit_date",
}

REVIEW_SUGGESTION_TIERS = {"primary", "backlog", "all"}
CLOSED_REVIEW_STATUSES = {"confirmed", "rejected", "excluded", "merged"}

AUTO_ACCEPT_POLICIES = {"safe", "legacy"}

# Per-action confidence floors for the "safe" auto-accept policy. Actions that
# only add conservative/reversible context (role-only, historical) have no
# floor. Identity-changing actions need stronger evidence than event labels.
SAFE_AUTO_ACCEPT_MIN_CONFIDENCE: dict[str, float | None] = {
    "confirm_identity": 0.75,
    "confirm_speaker_identity": 0.7,
    "confirm_person": 0.6,
    "merge_person": 0.7,
    "mark_role_only": None,
    "confirm_place": 0.65,
    "confirm_place_context": 0.6,
    "confirm_geocode_later": None,
    "confirm_relationship": 0.75,
    "confirm_event": 0.55,
    "move_event_range": 0.85,
    "mark_unrelated": 0.7,
    "confirm_event_date": 0.65,
    "mark_historical_context": None,
}

# Relationships based only on a kinship word near a name are the known
# over-eager case. A single-mention candidate must clear a higher bar.
SAFE_RELATIONSHIP_SINGLE_EVIDENCE_MIN_CONFIDENCE = 0.85

# Per-face triage is a human judgment about which crops belong together. No
# generator suggests these and no policy may auto-accept them: they are
# ground truth about cluster quality, not automatable inferences.
HUMAN_ONLY_REVIEW_ACTIONS = {
    "detach_faces_from_cluster",
    "reassign_face_observations",
    "mark_face_unknown",
}

# Face decisions bind to media spans, not just to cluster/observation ids:
# cluster ids renumber on re-cluster and observation ids on re-detection, so
# every face correction carries payload.anchors {media_id, span, bbox} and the
# replay path resolves anchors back to current rows by time + bbox IoU.
FACE_ANCHOR_TARGET_FILES = {"face_clusters.jsonl", "face_identity_candidates.jsonl"}
FACE_ANCHOR_TIME_TOLERANCE_S = 0.5
FACE_ANCHOR_MIN_IOU = 0.5
# An anchored cluster reference resolves only when at least half of its
# resolvable anchors agree on one current cluster.
FACE_ANCHOR_MIN_CLUSTER_FRACTION = 0.5

# Generated rows are numbered by sort rank and renumber on every rebuild, so a
# correction that only stores target_id lands on whatever row took that number.
# Every correction also stores a content fingerprint of its target (and of any
# other generated row its payload names). Replay resolves the fingerprint
# against the current rows and skips the correction when it matches nothing or
# more than one row, instead of changing the wrong record.
FINGERPRINT_TARGET_FILES = {
    "canonical_events.jsonl",
    "context_edges.jsonl",
    "date_groups.jsonl",
    "people_groups.jsonl",
    "place_groups.jsonl",
    "relationship_candidates.jsonl",
    "speaker_identity_candidates.jsonl",
}
# Payload fields that name other generated rows, and the file each lives in.
PAYLOAD_REF_FILES = {
    "merge_with_person_group_id": "people_groups.jsonl",
    "destination_person_group_id": "people_groups.jsonl",
    "person_group_id": "people_groups.jsonl",
    "subject_entity_id": "people_groups.jsonl",
    "object_entity_id": "people_groups.jsonl",
}
# Machine reviewers whose id-only corrections the drift migration quarantines.
MACHINE_REVIEWERS = {"auto-pipeline", "bulk-suggestion", "review-ui-bulk"}
_ROLE_KEY = re.compile(r"^role:([^:]+):(canonical_event_\d+)$")


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
        _attach_face_anchors(state, correction)
        _attach_fingerprints(state, correction)
        effects = _apply_action(state, correction)
        correction["applied_effects"] = effects
        corrections.append(correction)
        normalized_actions.append(correction)
        touched_files.update(effect["file"] for effect in effects if effect.get("file"))

    state.write(touched_files)
    _write_jsonl(project / "corrections.jsonl", corrections)
    dual = DualWriter.open(project, artifact="corrections.jsonl", producer="human")
    for correction in normalized_actions:
        dual.write_row(
            correction,
            kind="human_action",
            media_id=None,
            assertion={"action": correction.get("action"), "target_id": correction.get("target_id")},
            producer=str(correction.get("reviewer") or "human"),
        )
    dual.close()
    return {
        "project": str(project),
        "actions_applied": len(normalized_actions),
        "by_action": _count_by(normalized_actions, "action"),
        "touched_files": sorted(touched_files | {"corrections.jsonl"}),
        "outputs": {"corrections": str(project / "corrections.jsonl")},
    }


def apply_review_suggestions(
    project_dir: Path,
    *,
    tier: str = "primary",
    min_confidence: float | None = None,
    dry_run: bool = False,
    reviewer: str = "bulk-suggestion",
    policy: str = "safe",
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    selected, skipped = _collect_review_suggestion_actions(
        project,
        tier=tier,
        min_confidence=min_confidence,
        reviewer=reviewer,
        policy=policy,
    )
    selected, already = _drop_already_decided(project, selected)
    skipped = [*skipped, *already]
    summary: dict[str, Any] = {
        "project": str(project),
        "tier": tier,
        "policy": policy,
        "dry_run": dry_run,
        "suggestions_selected": len(selected),
        "by_action": _count_by(selected, "action"),
        "skipped": skipped,
    }
    if dry_run or not selected:
        summary["actions_applied"] = 0
        summary["selected_actions"] = selected
        return summary

    applied = apply_review_actions(project, actions=selected)
    return {**applied, **summary, "actions_applied": applied["actions_applied"]}


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

    superseded_count = 0
    for correction in corrections:
        if correction.get("superseded"):
            # M3 remediation: machine-made corrections marked superseded are
            # never replayed — re-derivation under the current model starts
            # from a clean slate instead of re-cementing old conflations.
            superseded_count += 1
            continue
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
        "corrections_superseded": superseded_count,
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
            "speaker_identity_candidates.jsonl": read_jsonl(project / "speaker_identity_candidates.jsonl"),
            "speaker_segments.jsonl": read_jsonl(project / "speaker_segments.jsonl"),
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
        "label_face_cluster": _label_face_cluster,
        "reject_identity": _reject_identity,
        "detach_faces_from_cluster": _detach_faces_from_cluster,
        "reassign_face_observations": _reassign_face_observations,
        "mark_face_unknown": _mark_face_unknown,
        "confirm_speaker_identity": _confirm_speaker_identity,
        "reject_speaker_identity": _reject_speaker_identity,
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
        "move_event_range": _move_event_range,
        "rename_event": _rename_event,
        "split_event": _request_manual_split,
        "mark_unrelated": _mark_unrelated,
        "confirm_event_date": _confirm_event_date,
        "mark_historical_context": _mark_historical_context,
        "edit_date": _edit_date,
    }
    return dispatch[action](state, correction)


def _label_face_cluster(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    """Name a face cluster directly — for people no candidate matched."""

    cluster = _require_target(state, "face_clusters.jsonl", correction)
    payload = correction["payload"]
    label = str(payload.get("label") or "").strip()
    if not label:
        raise ValueError("label_face_cluster requires payload.label")
    _apply_label_payload(cluster, {"label": label})
    if payload.get("person_group_id"):
        cluster["linked_person_group_id"] = str(payload["person_group_id"])
    _mark_reviewed(cluster, "confirmed", correction)
    return [_effect("face_clusters.jsonl", cluster, f"named face cluster {label}")]


def _move_event_range(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    """Snap an event to the tape range where its content actually lives."""

    event = _require_target(state, "canonical_events.jsonl", correction)
    payload = correction["payload"]
    source_video_id = str(payload.get("source_video_id") or "")
    start_s = payload.get("start_s")
    end_s = payload.get("end_s")
    if not source_video_id or start_s is None or end_s is None:
        raise ValueError("move_event_range requires source_video_id, start_s, and end_s")
    start_s = float(start_s)
    end_s = float(end_s)

    new_range = {"source_video_id": source_video_id, "start_s": round(start_s, 3), "end_s": round(end_s, 3)}
    metadata = _metadata(event)
    previous = metadata.get("source_ranges")
    if previous:
        metadata["pre_regrounding_source_ranges"] = previous
    metadata["source_ranges"] = [new_range]
    metadata["source_video_ids"] = [source_video_id]
    metadata["regrounded"] = True
    event["source_ranges"] = [new_range]
    event["source_video_ids"] = [source_video_id]
    # Timeline seconds shift with the range; stitch offsets are re-derived on
    # the next rebuild, so store per-video seconds here.
    event["start_s"] = round(start_s, 3)
    event["end_s"] = round(end_s, 3)
    _mark_reviewed(event, "confirmed", correction)
    return [_effect("canonical_events.jsonl", event, f"moved event to {source_video_id} {start_s:.0f}-{end_s:.0f}s")]


def _confirm_identity(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _require_target(state, "face_identity_candidates.jsonl", correction)
    payload = correction["payload"]
    person_group_id = _resolve_ref(state, correction, "person_group_id") or str(candidate.get("person_group_id") or "")
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


def _requested_cluster_faces(
    state: _ProjectReviewState,
    cluster: dict[str, Any],
    correction: dict[str, Any],
    *,
    require_membership: bool = True,
) -> list[dict[str, Any]]:
    requested = [str(value) for value in correction["payload"].get("face_observation_ids") or [] if value]
    if not requested:
        raise ValueError(f"{correction['action']} requires payload.face_observation_ids")
    cluster_id = str(cluster.get("id") or "")
    wanted = set(requested)
    faces = [
        face
        for face in state.rows("face_observations.jsonl")
        if str(face.get("id") or "") in wanted
        and (not require_membership or str(face.get("face_cluster_id") or "") == cluster_id)
    ]
    if not faces:
        # Observation ids renumber on re-detection; fall back to the span
        # anchors stamped when the human made the call.
        anchors = [
            anchor
            for anchor in correction["payload"].get("anchors") or []
            if str(anchor.get("face_observation_id") or "") in wanted
        ]
        seen: set[str] = set()
        for anchor in anchors:
            observation = _resolve_face_observation_by_anchor(state, anchor)
            if not observation:
                continue
            face_id = str(observation.get("id") or "")
            if face_id in seen:
                continue
            if require_membership and str(observation.get("face_cluster_id") or "") != cluster_id:
                continue
            seen.add(face_id)
            faces.append(observation)
    if not faces:
        raise ValueError(f"none of the requested face observations belong to {cluster_id}")
    return faces


def _remove_faces_from_cluster_row(
    cluster: dict[str, Any],
    removed_ids: set[str],
    correction: dict[str, Any],
) -> None:
    remaining = [fid for fid in cluster.get("face_observation_ids") or [] if str(fid) not in removed_ids]
    cluster["face_observation_ids"] = remaining
    cluster["face_count"] = len(remaining)
    if str(cluster.get("representative_face_observation_id") or "") in removed_ids:
        cluster["representative_face_observation_id"] = remaining[0] if remaining else ""
    if not remaining:
        cluster["review_only"] = True
        _append_unique(cluster, "notes", "all faces removed by reviewer")
    _append_unique(cluster, "review_correction_ids", correction["id"])


def _detach_faces_from_cluster(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    """Reviewer says these crops are not the same person: return them to the pool."""

    cluster = _require_target(state, "face_clusters.jsonl", correction)
    faces = _requested_cluster_faces(state, cluster, correction)
    cluster_id = str(cluster.get("id") or "")
    effects = []
    for face in faces:
        face["face_cluster_id"] = ""
        face["detached_from_face_cluster_id"] = cluster_id
        face["person_group_id"] = ""
        face["identity_review_status"] = "detached"
        _append_unique(face, "review_correction_ids", correction["id"])
        effects.append(_effect("face_observations.jsonl", face, "detached face from cluster"))
    _remove_faces_from_cluster_row(cluster, {str(face["id"]) for face in faces}, correction)
    effects.append(_effect("face_clusters.jsonl", cluster, f"detached {len(faces)} face(s) from cluster"))
    return effects


def _reassign_face_observations(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    """Reviewer says these crops belong to a different cluster or person."""

    cluster = _require_target(state, "face_clusters.jsonl", correction)
    payload = correction["payload"]
    destination_cluster_id = str(payload.get("destination_face_cluster_id") or "")
    destination_person_id = str(payload.get("destination_person_group_id") or "")
    if not destination_cluster_id and not destination_person_id:
        raise ValueError(
            "reassign_face_observations requires destination_face_cluster_id or destination_person_group_id"
        )
    destination_cluster = (
        state.row_by_id("face_clusters.jsonl", destination_cluster_id) if destination_cluster_id else None
    )
    if destination_cluster_id and not destination_cluster:
        raise ValueError(f"{destination_cluster_id} not found in face_clusters.jsonl")
    if not destination_person_id and destination_cluster:
        destination_person_id = str(destination_cluster.get("linked_person_group_id") or "")

    faces = _requested_cluster_faces(state, cluster, correction)
    source_id = str(cluster.get("id") or "")
    effects = []
    for face in faces:
        face["face_cluster_id"] = destination_cluster_id
        face["detached_from_face_cluster_id"] = source_id
        face["person_group_id"] = destination_person_id
        face["identity_review_status"] = "reassigned"
        _append_unique(face, "review_correction_ids", correction["id"])
        effects.append(_effect("face_observations.jsonl", face, "reassigned face observation"))
    moved_ids = {str(face["id"]) for face in faces}
    _remove_faces_from_cluster_row(cluster, moved_ids, correction)
    effects.append(_effect("face_clusters.jsonl", cluster, f"moved {len(faces)} face(s) out of cluster"))

    if destination_cluster:
        for face_id in sorted(moved_ids):
            _append_unique(destination_cluster, "face_observation_ids", face_id)
        destination_cluster["face_count"] = len(destination_cluster.get("face_observation_ids") or [])
        _append_unique(destination_cluster, "review_correction_ids", correction["id"])
        effects.append(_effect("face_clusters.jsonl", destination_cluster, "received reassigned face(s)"))

    if destination_person_id:
        person = state.row_by_id("people_groups.jsonl", destination_person_id)
        if person:
            _append_unique(person, "review_correction_ids", correction["id"])
            effects.append(_effect("people_groups.jsonl", person, "faces reassigned to person"))
    return effects


def _mark_face_unknown(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    """Reviewer honestly does not know who this is: exclude from identity suggestions."""

    cluster = _require_target(state, "face_clusters.jsonl", correction)
    faces = _requested_cluster_faces(state, cluster, correction, require_membership=False)
    effects = []
    for face in faces:
        face["identity_unknown"] = True
        face["person_group_id"] = ""
        face["identity_review_status"] = "unknown"
        _append_unique(face, "review_correction_ids", correction["id"])
        effects.append(_effect("face_observations.jsonl", face, "marked face as unknown identity"))
    _append_unique(cluster, "notes", f"{len(faces)} face(s) marked unknown by reviewer")
    _append_unique(cluster, "review_correction_ids", correction["id"])
    effects.append(_effect("face_clusters.jsonl", cluster, "recorded unknown-identity faces"))
    return effects


def _confirm_speaker_identity(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _require_target(state, "speaker_identity_candidates.jsonl", correction)
    payload = correction["payload"]
    speaker_label = str(payload.get("speaker_label") or candidate.get("speaker_label") or "")
    person_group_id = _resolve_ref(state, correction, "person_group_id") or str(candidate.get("person_group_id") or "")
    if not speaker_label or not person_group_id:
        raise ValueError("confirm_speaker_identity requires speaker_label and person_group_id")

    effects = []
    candidate["speaker_label"] = speaker_label
    candidate["person_group_id"] = person_group_id
    _mark_reviewed(candidate, "confirmed", correction)
    effects.append(_effect("speaker_identity_candidates.jsonl", candidate, "confirmed speaker identity candidate"))

    for segment in state.rows("speaker_segments.jsonl"):
        if str(segment.get("speaker_label") or "") != speaker_label:
            continue
        segment["person_group_id"] = person_group_id
        segment["speaker_identity_review_status"] = "confirmed"
        segment["speaker_identity_candidate_id"] = candidate.get("id")
        _append_unique(segment, "review_correction_ids", correction["id"])
        effects.append(_effect("speaker_segments.jsonl", segment, "linked speaker segment to person"))

    person = state.row_by_id("people_groups.jsonl", person_group_id)
    if person:
        _append_unique(person, "confirmed_speaker_labels", speaker_label)
        _append_unique(person, "confirmed_speaker_identity_candidate_ids", candidate.get("id"))
        _append_unique(person, "review_correction_ids", correction["id"])
        effects.append(_effect("people_groups.jsonl", person, "stored confirmed speaker on person"))
    return effects


def _reject_speaker_identity(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _require_target(state, "speaker_identity_candidates.jsonl", correction)
    _mark_reviewed(candidate, "rejected", correction)
    return [_effect("speaker_identity_candidates.jsonl", candidate, "rejected speaker identity candidate")]


def _confirm_person(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    person = _require_target(state, "people_groups.jsonl", correction)
    _apply_label_payload(person, correction["payload"])
    _mark_reviewed(person, "confirmed", correction)
    return [_effect("people_groups.jsonl", person, "confirmed person")]


def _merge_person(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    source = _require_target(state, "people_groups.jsonl", correction)
    payload = correction["payload"]
    field = "merge_with_person_group_id" if payload.get("merge_with_person_group_id") else "destination_person_group_id"
    destination_id = _resolve_ref(state, correction, field)
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
    _apply_relationship_payload(relationship, _payload_with_resolved_refs(state, correction))
    _mark_reviewed(relationship, "confirmed", correction)
    return [_effect("relationship_candidates.jsonl", relationship, "confirmed relationship")]


def _reject_relationship(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    relationship = _require_target(state, "relationship_candidates.jsonl", correction)
    _mark_reviewed(relationship, "rejected", correction)
    return [_effect("relationship_candidates.jsonl", relationship, "rejected relationship")]


def _edit_relationship(state: _ProjectReviewState, correction: dict[str, Any]) -> list[dict[str, Any]]:
    relationship = _require_target(state, "relationship_candidates.jsonl", correction)
    _apply_relationship_payload(relationship, _payload_with_resolved_refs(state, correction))
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


def _payload_with_resolved_refs(state: _ProjectReviewState, correction: dict[str, Any]) -> dict[str, Any]:
    payload = dict(correction.get("payload") or {})
    for field in ("subject_entity_id", "object_entity_id"):
        if payload.get(field) and field in (correction.get("ref_fingerprints") or {}):
            payload[field] = _resolve_ref(state, correction, field)
    return payload


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
    expected = correction.get("target_fingerprint")
    if expected and filename in FINGERPRINT_TARGET_FILES:
        return _row_for_fingerprint(state, filename, str(expected), target_id)
    row = state.row_by_id(filename, target_id)
    if not row and filename in FACE_ANCHOR_TARGET_FILES:
        row = _resolve_face_target_by_anchors(state, filename, correction)
    if not row:
        raise ValueError(f"{target_id} not found in {filename}")
    return row


def _row_for_fingerprint(state: _ProjectReviewState, filename: str, expected: str, row_id: str) -> dict[str, Any]:
    """The one current row with this fingerprint; the row at row_id wins when it still matches."""

    events_by_id = _events_by_id(state)
    row = state.row_by_id(filename, row_id) if row_id else None
    if row is not None and _row_fingerprint(filename, row, events_by_id) == expected:
        return row
    matches = [candidate for candidate in state.rows(filename) if _row_fingerprint(filename, candidate, events_by_id) == expected]
    if len(matches) == 1:
        return matches[0]
    reason = "matches several rows" if matches else "matches no row"
    raise ValueError(f"{row_id or expected} not found in {filename} (fingerprint {reason})")


def _resolve_ref(state: _ProjectReviewState, correction: dict[str, Any], field: str) -> str:
    """The current id for a payload field that names another generated row."""

    payload = correction.get("payload") or {}
    raw = str(payload.get(field) or "")
    expected = (correction.get("ref_fingerprints") or {}).get(field)
    filename = PAYLOAD_REF_FILES.get(field)
    if not expected or not filename:
        return raw
    return str(_row_for_fingerprint(state, filename, str(expected), raw).get("id") or "")


def _attach_fingerprints(state: _ProjectReviewState, correction: dict[str, Any]) -> None:
    """Stamp a correction with content fingerprints of its target and payload references."""

    events_by_id = _events_by_id(state)

    def unique_fingerprint(filename: str, row_id: str) -> str | None:
        row = state.row_by_id(filename, row_id) if row_id else None
        fingerprint = _row_fingerprint(filename, row, events_by_id) if row else None
        if not fingerprint:
            return None
        twins = sum(1 for other in state.rows(filename) if _row_fingerprint(filename, other, events_by_id) == fingerprint)
        return fingerprint if twins == 1 else None

    filename = _target_filename(correction)
    if filename in FINGERPRINT_TARGET_FILES and not correction.get("target_fingerprint"):
        fingerprint = unique_fingerprint(filename, str(correction.get("target_id") or ""))
        if fingerprint:
            correction["target_fingerprint"] = fingerprint
    refs = dict(correction.get("ref_fingerprints") or {})
    for field, ref_file in PAYLOAD_REF_FILES.items():
        ref_id = str((correction.get("payload") or {}).get(field) or "")
        if not ref_id or field in refs:
            continue
        fingerprint = unique_fingerprint(ref_file, ref_id)
        if fingerprint:
            refs[field] = fingerprint
    if refs:
        correction["ref_fingerprints"] = refs


def _events_by_id(state: _ProjectReviewState) -> dict[str, dict[str, Any]]:
    return {str(event.get("id") or ""): event for event in state.rows("canonical_events.jsonl")}


def _row_fingerprint(filename: str, row: dict[str, Any], events_by_id: dict[str, dict[str, Any]]) -> str | None:
    """A content key that survives renumbering. Never built from labels a correction can change."""

    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if filename == "people_groups.jsonl":
        key = str(metadata.get("normalized_key") or "")
        role = _ROLE_KEY.match(key)
        if role:
            span = _event_span_key(events_by_id.get(role.group(2)))
            key = f"role:{role.group(1)}:{span}" if span else ""
        return f"person:{key}" if key else None
    if filename == "place_groups.jsonl":
        key = str(metadata.get("resolution_key") or "")
        return f"place:{key}" if key else None
    if filename == "date_groups.jsonl":
        key = str(metadata.get("normalized_key") or "")
        return f"date:{key}" if key else None
    if filename == "canonical_events.jsonl":
        span = _event_span_key(row)
        return f"event:{span}" if span else None
    if filename == "relationship_candidates.jsonl":
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        parts = [str(row.get("predicate") or ""), _fingerprint_text(row.get("subject_label")),
                 _fingerprint_text(row.get("object_label")), _scope_key(scope)]
        return "relationship:" + "|".join(parts) if all(parts[:3]) else None
    if filename == "speaker_identity_candidates.jsonl":
        label = str(row.get("speaker_label") or "")
        person = _fingerprint_text(row.get("person_label"))
        return f"speaker:{label}|{person}" if label and person else None
    if filename == "context_edges.jsonl":
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        parts = [str(row.get("predicate") or ""), _fingerprint_text(row.get("subject_label")),
                 _fingerprint_text(row.get("object_label")), _scope_key(scope)]
        return "edge:" + "|".join(parts) if all(parts[:3]) else None
    return None


def _event_span_key(event: dict[str, Any] | None) -> str:
    if not event:
        return ""
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    ranges = metadata.get("source_ranges") or event.get("source_ranges") or []
    spans = sorted(
        f"{item.get('source_video_id')}@{round(float(item.get('start_s') or 0))}-{round(float(item.get('end_s') or 0))}"
        for item in ranges
        if isinstance(item, dict) and item.get("source_video_id")
    )
    return ",".join(spans)


def _scope_key(scope: dict[str, Any]) -> str:
    videos = ",".join(sorted(str(video) for video in scope.get("source_video_ids") or []))
    start = scope.get("start_s")
    return f"{videos}@{round(float(start))}" if videos and start is not None else videos


def _fingerprint_text(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", str(value or "").casefold())).strip()


def _drop_already_decided(project: Path, actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Skip suggestions whose decision is already on file for the same content."""

    decided = {
        (str(row.get("action") or ""), str(row.get("target_fingerprint") or ""))
        for row in read_jsonl(project / "corrections.jsonl")
        if not row.get("superseded") and row.get("target_fingerprint")
    }
    if not decided or not actions:
        return actions, []
    state = _ProjectReviewState(project)
    events_by_id = _events_by_id(state)
    kept, skipped = [], []
    for action in actions:
        target_id = str(action.get("target_id") or "")
        filename = _target_filename({"target_id": target_id, "target_type": action.get("target_type")})
        row = state.row_by_id(filename, target_id) if filename in FINGERPRINT_TARGET_FILES else None
        fingerprint = _row_fingerprint(filename, row, events_by_id) if row else None
        if fingerprint and (str(action.get("action") or ""), fingerprint) in decided:
            skipped.append({"target_id": target_id, "action": action.get("action"), "reason": "already decided"})
            continue
        kept.append(action)
    return kept, skipped


def quarantine_unfingerprinted_corrections(project_dir: Path, *, reason: str = "id_drift_quarantine") -> dict[str, Any]:
    """Stop replaying machine-made corrections that only know their target by a positional id.

    Those ids renumber on every rebuild, so such corrections now land on other
    records. They were all made by the pipeline itself (nothing human is lost);
    replay skips superseded rows, and the next auto-accept pass re-derives the
    decisions with fingerprints. Idempotent.
    """

    project = project_dir.expanduser().resolve()
    path = project / "corrections.jsonl"
    rows = read_jsonl(path)
    stamped = _now_iso()
    quarantined = 0
    for row in rows:
        if row.get("superseded") or row.get("target_fingerprint"):
            continue
        if str(row.get("reviewer") or "") not in MACHINE_REVIEWERS:
            continue
        row["superseded"] = True
        row["superseded_by"] = reason
        row["superseded_at"] = stamped
        row["superseded_reason"] = "target known only by a positional id that renumbers on rebuild"
        quarantined += 1
    if quarantined:
        _write_jsonl(path, rows)
    return {"project": str(project), "quarantined": quarantined, "total": len(rows)}


def _attach_face_anchors(state: _ProjectReviewState, correction: dict[str, Any]) -> None:
    """Stamp face corrections with media-span anchors at decision time."""

    filename = _target_filename(correction)
    if filename not in FACE_ANCHOR_TARGET_FILES:
        return
    payload = correction["payload"]
    if payload.get("anchors"):
        return
    cluster_id = ""
    if filename == "face_identity_candidates.jsonl":
        candidate = state.row_by_id(filename, str(correction.get("target_id") or ""))
        if candidate:
            cluster_id = str(candidate.get("face_cluster_id") or "")
    if not cluster_id:
        cluster_id = str(payload.get("face_cluster_id") or "")
    if not cluster_id and filename == "face_clusters.jsonl":
        cluster_id = str(correction.get("target_id") or "")

    requested_ids = {str(value) for value in payload.get("face_observation_ids") or [] if value}
    observations = []
    for face in state.rows("face_observations.jsonl"):
        face_id = str(face.get("id") or "")
        if requested_ids:
            if face_id in requested_ids:
                observations.append(face)
        elif cluster_id and str(face.get("face_cluster_id") or "") == cluster_id:
            observations.append(face)
    anchors = [face_anchor(face) for face in observations]
    anchors = [anchor for anchor in anchors if anchor.get("media_id")]
    if anchors:
        payload["anchors"] = anchors
        if cluster_id:
            payload.setdefault("anchor_cluster_id", cluster_id)


def _resolve_face_target_by_anchors(
    state: _ProjectReviewState, filename: str, correction: dict[str, Any]
) -> dict[str, Any] | None:
    anchors = correction.get("payload", {}).get("anchors") or []
    if not anchors:
        return None
    cluster = _cluster_from_anchors(state, anchors)
    if not cluster:
        return None
    if filename == "face_clusters.jsonl":
        return cluster
    if filename == "face_identity_candidates.jsonl":
        cluster_id = str(cluster.get("id") or "")
        person_group_id = str(correction.get("payload", {}).get("person_group_id") or "")
        candidates = [
            row
            for row in state.rows("face_identity_candidates.jsonl")
            if str(row.get("face_cluster_id") or "") == cluster_id
        ]
        if person_group_id:
            candidates = [
                row for row in candidates if str(row.get("person_group_id") or "") == person_group_id
            ] or candidates
        return candidates[0] if candidates else None
    return None


def _cluster_from_anchors(state: _ProjectReviewState, anchors: list[dict[str, Any]]) -> dict[str, Any] | None:
    votes: dict[str, int] = {}
    resolved = 0
    for anchor in anchors:
        observation = _resolve_face_observation_by_anchor(state, anchor)
        if not observation:
            continue
        resolved += 1
        cluster_id = str(observation.get("face_cluster_id") or "")
        if cluster_id:
            votes[cluster_id] = votes.get(cluster_id, 0) + 1
    if not votes or not resolved:
        return None
    cluster_id, count = max(votes.items(), key=lambda item: (item[1], item[0]))
    if count / resolved < FACE_ANCHOR_MIN_CLUSTER_FRACTION:
        return None
    return state.row_by_id("face_clusters.jsonl", cluster_id)


def _resolve_face_observation_by_anchor(
    state: _ProjectReviewState, anchor: dict[str, Any]
) -> dict[str, Any] | None:
    media_id = str(anchor.get("media_id") or "")
    span = anchor.get("span") or {}
    anchor_time = _float_or_none(span.get("start_s"))
    anchor_bbox = anchor.get("bbox") or {}
    if not media_id or anchor_time is None:
        return None
    best = None
    best_iou = 0.0
    for face in state.rows("face_observations.jsonl"):
        if str(face.get("source_video_id") or "") != media_id:
            continue
        face_time = _float_or_none(face.get("time_s"))
        if face_time is None:
            face_time = _float_or_none(face.get("start_s"))
        if face_time is None or abs(face_time - anchor_time) > FACE_ANCHOR_TIME_TOLERANCE_S:
            continue
        iou = _bbox_iou(anchor_bbox, face.get("bbox") or {})
        if iou >= FACE_ANCHOR_MIN_IOU and iou > best_iou:
            best = face
            best_iou = iou
    return best


def _bbox_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    try:
        lx, ly = float(left.get("x") or 0), float(left.get("y") or 0)
        lw, lh = float(left.get("width") or 0), float(left.get("height") or 0)
        rx, ry = float(right.get("x") or 0), float(right.get("y") or 0)
        rw, rh = float(right.get("width") or 0), float(right.get("height") or 0)
    except (TypeError, ValueError):
        return 0.0
    if lw <= 0 or lh <= 0 or rw <= 0 or rh <= 0:
        return 0.0
    inter_w = min(lx + lw, rx + rw) - max(lx, rx)
    inter_h = min(ly + lh, ry + rh) - max(ly, ry)
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    intersection = inter_w * inter_h
    union = lw * lh + rw * rh - intersection
    return intersection / union if union > 0 else 0.0


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def annotate_face_corrections_with_anchors(project_dir: Path) -> dict[str, Any]:
    """One-shot migration: stamp existing face corrections with span anchors.

    Free while corrections are machine-made; run before re-clustering so
    replay survives the id churn. Idempotent.
    """

    project = project_dir.expanduser().resolve()
    corrections = read_jsonl(project / "corrections.jsonl")
    if not corrections:
        return {"project": str(project), "corrections": 0, "annotated": 0}
    state = _ProjectReviewState(project)
    annotated = 0
    for correction in corrections:
        payload = correction.setdefault("payload", {})
        if payload.get("anchors"):
            continue
        before = bool(payload.get("anchors"))
        _attach_face_anchors(state, correction)
        if not before and payload.get("anchors"):
            annotated += 1
    if annotated:
        _write_jsonl(project / "corrections.jsonl", corrections)
    return {"project": str(project), "corrections": len(corrections), "annotated": annotated}


def _is_missing_target_error(exc: ValueError) -> bool:
    return " not found in " in str(exc)


def _target_filename(correction: dict[str, Any]) -> str:
    target_type = str(correction.get("target_type") or _infer_target_type(str(correction.get("target_id") or "")))
    return {
        "context_edge": "context_edges.jsonl",
        "date_group": "date_groups.jsonl",
        "event": "canonical_events.jsonl",
        "face_cluster": "face_clusters.jsonl",
        "face_identity_candidate": "face_identity_candidates.jsonl",
        "people_group": "people_groups.jsonl",
        "place_group": "place_groups.jsonl",
        "relationship_candidate": "relationship_candidates.jsonl",
        "speaker_identity_candidate": "speaker_identity_candidates.jsonl",
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
        "speaker_identity_candidate_": "speaker_identity_candidate",
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


def _collect_review_suggestion_actions(
    project: Path,
    *,
    tier: str,
    min_confidence: float | None,
    reviewer: str,
    policy: str = "safe",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if tier not in REVIEW_SUGGESTION_TIERS:
        raise ValueError(f"tier must be one of: {', '.join(sorted(REVIEW_SUGGESTION_TIERS))}")
    if policy not in AUTO_ACCEPT_POLICIES:
        raise ValueError(f"policy must be one of: {', '.join(sorted(AUTO_ACCEPT_POLICIES))}")

    review_items = _load_review_items_for_tier(project, tier)
    state = _ProjectReviewState(project)
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_target_ids: set[str] = set()
    tuned_floors: dict[str, float] = {}
    if policy == "safe":
        from tapesplit.calibration import load_review_policy_floors

        tuned_floors = load_review_policy_floors(project)

    for item in review_items:
        item_id = str(item.get("id") or item.get("source_id") or "")
        if _is_review_closed(item):
            skipped.append({"item_id": item_id, "reason": "review item already closed"})
            continue
        suggestion = item.get("suggested_action") if isinstance(item.get("suggested_action"), dict) else {}
        if not suggestion:
            skipped.append({"item_id": item_id, "reason": "no suggested action"})
            continue
        confidence = _number_or_none(suggestion.get("confidence")) or _number_or_none(item.get("confidence"))
        if min_confidence is not None and (confidence is None or confidence < min_confidence):
            skipped.append(
                {
                    "item_id": item_id,
                    "target_id": suggestion.get("target_id"),
                    "reason": "below min confidence",
                    "confidence": confidence,
                }
            )
            continue

        for action in _actions_from_suggestion(item, suggestion, reviewer=reviewer):
            target_id = str(action.get("target_id") or "")
            if not target_id:
                skipped.append({"item_id": item_id, "reason": "suggested action missing target_id"})
                continue
            if target_id in seen_target_ids:
                skipped.append({"item_id": item_id, "target_id": target_id, "reason": "duplicate target"})
                continue
            target_filename = _target_filename(action)
            target_row = state.row_by_id(target_filename, target_id) if target_filename else None
            if target_filename and target_row is None:
                skipped.append({"item_id": item_id, "target_id": target_id, "reason": "target missing"})
                continue
            if target_row is not None and _is_review_closed(target_row):
                skipped.append({"item_id": item_id, "target_id": target_id, "reason": "target already closed"})
                continue
            if policy == "safe":
                block_reason = _safe_policy_block_reason(action, confidence, target_row, tuned_floors)
                if block_reason:
                    skipped.append(
                        {
                            "item_id": item_id,
                            "target_id": target_id,
                            "action": action.get("action"),
                            "reason": f"safe policy: {block_reason}",
                            "confidence": confidence,
                        }
                    )
                    continue
            seen_target_ids.add(target_id)
            selected.append(action)

    return selected, skipped


def _safe_policy_block_reason(
    action: dict[str, Any],
    confidence: float | None,
    target_row: dict[str, Any] | None,
    tuned_floors: dict[str, float] | None = None,
) -> str | None:
    """Return why the safe auto-accept policy refuses this action, or None."""

    action_name = str(action.get("action") or "")
    if action_name in HUMAN_ONLY_REVIEW_ACTIONS:
        return f"{action_name} is human-only and never auto-accepted"
    floor = SAFE_AUTO_ACCEPT_MIN_CONFIDENCE.get(action_name, 0.7)
    if tuned_floors and action_name in tuned_floors and floor is not None:
        floor = tuned_floors[action_name]
    if floor is not None:
        if confidence is None:
            return f"{action_name} has no confidence (needs >= {floor})"
        if confidence < floor:
            return f"confidence {confidence} below {action_name} floor {floor}"

    if action_name == "confirm_relationship":
        return _relationship_block_reason(action, target_row)
    if action_name == "confirm_place_context":
        return _place_context_block_reason(target_row)
    return None


def _place_context_block_reason(target_row: dict[str, Any] | None) -> str | None:
    """The M3 contradiction guard: a context claim whose geographic parent is
    disjoint from the place's anchored parent is never auto-acceptable."""

    row = target_row if isinstance(target_row, dict) else {}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if metadata.get("contradiction") or row.get("contradiction"):
        notes = metadata.get("contradiction_notes") or row.get("contradiction_notes") or []
        detail = f" ({notes[0]})" if notes else ""
        return f"geographically contradicted context; carries both hypotheses for review{detail}"
    return None


def _relationship_block_reason(
    action: dict[str, Any],
    target_row: dict[str, Any] | None,
) -> str | None:
    row = target_row if isinstance(target_row, dict) else {}
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    subject = str(row.get("subject_entity_id") or payload.get("subject_entity_id") or "")
    object_ = str(row.get("object_entity_id") or payload.get("object_entity_id") or "")
    if not subject.startswith("people_group_") or not object_.startswith("people_group_"):
        return "needs a resolved person on both sides (role identity not bridged yet)"
    if row.get("contradicting_evidence_ids"):
        return "has contradicting evidence"
    evidence_count = len(row.get("evidence_ids") or [])
    confidence = _number_or_none(row.get("confidence")) or 0.0
    if evidence_count < 2 and confidence < SAFE_RELATIONSHIP_SINGLE_EVIDENCE_MIN_CONFIDENCE:
        return (
            "single evidence mention; needs a second corroborating mention or "
            f"confidence >= {SAFE_RELATIONSHIP_SINGLE_EVIDENCE_MIN_CONFIDENCE}"
        )
    return None


def _load_review_items_for_tier(project: Path, tier: str) -> list[dict[str, Any]]:
    visualization_path = project / "visualization.json"
    if not visualization_path.exists():
        raise ValueError(f"missing visualization.json in {project}; run tapesplit export-visualization first")
    payload = json.loads(visualization_path.read_text(encoding="utf-8"))
    primary = payload.get("review_queue") if isinstance(payload.get("review_queue"), list) else []
    backlog = payload.get("review_backlog") if isinstance(payload.get("review_backlog"), list) else []
    if tier == "primary":
        return list(primary)
    if tier == "backlog":
        return list(backlog)
    return [*primary, *backlog]


def _actions_from_suggestion(
    item: dict[str, Any],
    suggestion: dict[str, Any],
    *,
    reviewer: str,
) -> list[dict[str, Any]]:
    payload = dict(suggestion.get("payload") if isinstance(suggestion.get("payload"), dict) else {})
    target_type = str(suggestion.get("target_type") or item.get("source_record_type") or "")
    notes = str(suggestion.get("rationale") or "")
    item_id = str(item.get("id") or item.get("source_id") or "review_item")
    action_name = str(suggestion.get("action") or "")
    relationship_ids = [
        str(relationship_id)
        for relationship_id in payload.get("relationship_ids", [])
        if relationship_id
    ] if isinstance(payload.get("relationship_ids"), list) else []

    target_ids = relationship_ids if action_name == "confirm_relationship" and relationship_ids else [str(suggestion.get("target_id") or "")]
    return [
        {
            "id": f"{item_id}_{action_name}_{index:02d}",
            "action": action_name,
            "target_id": target_id,
            "target_type": target_type,
            "reviewer": reviewer,
            "notes": notes,
            "payload": payload,
        }
        for index, target_id in enumerate(target_ids, start=1)
    ]


def _is_review_closed(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").casefold() in CLOSED_REVIEW_STATUSES


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

"""Per-face triage: detach, reassign, and mark-unknown are human-only actions."""

import json
from pathlib import Path

import pytest

from tapesplit.review_actions import (
    HUMAN_ONLY_REVIEW_ACTIONS,
    apply_review_actions,
    reapply_review_corrections,
    _safe_policy_block_reason,
)
from tapesplit.storage import read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "face_observations.jsonl",
        [
            {"id": "fo_1", "face_cluster_id": "fc_1", "person_group_id": "pg_1", "review_status": "clustered"},
            {"id": "fo_2", "face_cluster_id": "fc_1", "person_group_id": "pg_1", "review_status": "clustered"},
            {"id": "fo_3", "face_cluster_id": "fc_1", "person_group_id": "pg_1", "review_status": "clustered"},
            {"id": "fo_4", "face_cluster_id": "fc_2", "person_group_id": "", "review_status": "clustered"},
        ],
    )
    _write_jsonl(
        project / "face_clusters.jsonl",
        [
            {
                "id": "fc_1",
                "face_observation_ids": ["fo_1", "fo_2", "fo_3"],
                "face_count": 3,
                "representative_face_observation_id": "fo_1",
                "linked_person_group_id": "pg_1",
                "review_status": "needs_review",
            },
            {
                "id": "fc_2",
                "face_observation_ids": ["fo_4"],
                "face_count": 1,
                "representative_face_observation_id": "fo_4",
                "linked_person_group_id": "",
                "review_status": "needs_review",
            },
        ],
    )
    _write_jsonl(
        project / "people_groups.jsonl",
        [
            {"id": "pg_1", "label": "Filip", "aliases": ["Filip"], "review_status": "confirmed"},
            {"id": "pg_2", "label": "Ekaterina", "aliases": ["Ekaterina", "Katya"], "review_status": "confirmed"},
        ],
    )
    return project


def _rows_by_id(project: Path, filename: str) -> dict[str, dict]:
    return {str(row["id"]): row for row in read_jsonl(project / filename)}


def test_detach_faces_updates_cluster_and_observations(tmp_path: Path):
    project = _project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "detach_faces_from_cluster",
                "target_id": "fc_1",
                "target_type": "face_cluster",
                "reviewer": "phillip",
                "payload": {"face_observation_ids": ["fo_1", "fo_3"]},
            }
        ],
    )
    faces = _rows_by_id(project, "face_observations.jsonl")
    assert faces["fo_1"]["face_cluster_id"] == ""
    assert faces["fo_1"]["detached_from_face_cluster_id"] == "fc_1"
    assert faces["fo_1"]["person_group_id"] == ""
    assert faces["fo_1"]["identity_review_status"] == "detached"
    assert faces["fo_2"]["face_cluster_id"] == "fc_1"

    cluster = _rows_by_id(project, "face_clusters.jsonl")["fc_1"]
    assert cluster["face_observation_ids"] == ["fo_2"]
    assert cluster["face_count"] == 1
    # fo_1 was the representative and left the cluster.
    assert cluster["representative_face_observation_id"] == "fo_2"


def test_detach_all_faces_marks_cluster_review_only(tmp_path: Path):
    project = _project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "detach_faces_from_cluster",
                "target_id": "fc_1",
                "payload": {"face_observation_ids": ["fo_1", "fo_2", "fo_3"]},
            }
        ],
    )
    cluster = _rows_by_id(project, "face_clusters.jsonl")["fc_1"]
    assert cluster["face_count"] == 0
    assert cluster["review_only"] is True


def test_detach_requires_faces_from_that_cluster(tmp_path: Path):
    project = _project(tmp_path)
    with pytest.raises(ValueError, match="belong to fc_1"):
        apply_review_actions(
            project,
            actions=[
                {
                    "action": "detach_faces_from_cluster",
                    "target_id": "fc_1",
                    "payload": {"face_observation_ids": ["fo_4"]},
                }
            ],
        )


def test_reassign_to_cluster_moves_membership_and_person(tmp_path: Path):
    project = _project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "reassign_face_observations",
                "target_id": "fc_1",
                "payload": {
                    "face_observation_ids": ["fo_2"],
                    "destination_face_cluster_id": "fc_2",
                    "destination_person_group_id": "pg_2",
                },
            }
        ],
    )
    faces = _rows_by_id(project, "face_observations.jsonl")
    assert faces["fo_2"]["face_cluster_id"] == "fc_2"
    assert faces["fo_2"]["person_group_id"] == "pg_2"
    assert faces["fo_2"]["identity_review_status"] == "reassigned"

    clusters = _rows_by_id(project, "face_clusters.jsonl")
    assert "fo_2" not in clusters["fc_1"]["face_observation_ids"]
    assert clusters["fc_1"]["face_count"] == 2
    assert "fo_2" in clusters["fc_2"]["face_observation_ids"]
    assert clusters["fc_2"]["face_count"] == 2


def test_reassign_to_person_only_detaches_from_cluster(tmp_path: Path):
    project = _project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "reassign_face_observations",
                "target_id": "fc_1",
                "payload": {"face_observation_ids": ["fo_1"], "destination_person_group_id": "pg_2"},
            }
        ],
    )
    face = _rows_by_id(project, "face_observations.jsonl")["fo_1"]
    assert face["face_cluster_id"] == ""
    assert face["person_group_id"] == "pg_2"


def test_reassign_requires_destination(tmp_path: Path):
    project = _project(tmp_path)
    with pytest.raises(ValueError, match="destination"):
        apply_review_actions(
            project,
            actions=[
                {
                    "action": "reassign_face_observations",
                    "target_id": "fc_1",
                    "payload": {"face_observation_ids": ["fo_1"]},
                }
            ],
        )


def test_mark_face_unknown_keeps_cluster_membership(tmp_path: Path):
    project = _project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "mark_face_unknown",
                "target_id": "fc_1",
                "payload": {"face_observation_ids": ["fo_3"]},
            }
        ],
    )
    face = _rows_by_id(project, "face_observations.jsonl")["fo_3"]
    assert face["identity_unknown"] is True
    assert face["person_group_id"] == ""
    assert face["identity_review_status"] == "unknown"
    # Stays in the cluster: the crop is still the same face, we just do not know whose.
    cluster = _rows_by_id(project, "face_clusters.jsonl")["fc_1"]
    assert "fo_3" in cluster["face_observation_ids"]


def test_triage_actions_are_replayable(tmp_path: Path):
    project = _project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "detach_faces_from_cluster",
                "target_id": "fc_1",
                "payload": {"face_observation_ids": ["fo_1"]},
            }
        ],
    )
    # Simulate a rebuild regenerating the artifacts, then replay corrections.
    _write_jsonl(
        project / "face_observations.jsonl",
        [
            {"id": "fo_1", "face_cluster_id": "fc_1", "person_group_id": "pg_1", "review_status": "clustered"},
            {"id": "fo_2", "face_cluster_id": "fc_1", "person_group_id": "pg_1", "review_status": "clustered"},
        ],
    )
    _write_jsonl(
        project / "face_clusters.jsonl",
        [
            {
                "id": "fc_1",
                "face_observation_ids": ["fo_1", "fo_2"],
                "face_count": 2,
                "representative_face_observation_id": "fo_1",
                "review_status": "needs_review",
            }
        ],
    )
    result = reapply_review_corrections(project)
    assert result["corrections_applied"] >= 1
    face = _rows_by_id(project, "face_observations.jsonl")["fo_1"]
    assert face["face_cluster_id"] == ""
    assert face["identity_review_status"] == "detached"


def test_triage_actions_blocked_from_auto_accept():
    for action_name in HUMAN_ONLY_REVIEW_ACTIONS:
        reason = _safe_policy_block_reason({"action": action_name, "confidence": 0.99}, 0.99, None)
        assert reason is not None
        assert "human-only" in reason

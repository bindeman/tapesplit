"""Safe auto-accept policy: per-action confidence floors + relationship gates."""

import json
from pathlib import Path

import pytest

from tapesplit.review_actions import apply_review_suggestions
from tapesplit.storage import read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _item(item_id: str, target_id: str, target_type: str, action: str, payload: dict, confidence: float) -> dict:
    return {
        "id": item_id,
        "source_id": target_id,
        "source_record_type": target_type,
        "review_status": "needs_review",
        "confidence": confidence,
        "suggested_action": {
            "action": action,
            "target_id": target_id,
            "target_type": target_type,
            "label": "Best guess",
            "rationale": "Test suggestion",
            "confidence": confidence,
            "payload": payload,
        },
    }


def _write_viz(path: Path, items: list[dict]) -> None:
    payload = {"review_queue": items, "review_backlog": []}
    (path / "visualization.json").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _relationship_row(
    row_id: str,
    *,
    subject: str = "people_group_000001",
    object_: str = "people_group_000002",
    evidence: int = 2,
    confidence: float = 0.8,
    contradicting: bool = False,
) -> dict:
    return {
        "id": row_id,
        "review_status": "needs_review",
        "subject_entity_id": subject,
        "object_entity_id": object_,
        "evidence_ids": [f"ev_{i:06d}" for i in range(evidence)],
        "contradicting_evidence_ids": ["ev_bad"] if contradicting else [],
        "confidence": confidence,
    }


def _relationship_item(target_id: str, confidence: float = 0.9) -> dict:
    return _item(
        f"item_{target_id}",
        target_id,
        "relationship_candidate",
        "confirm_relationship",
        {"predicate": "mother_of"},
        confidence,
    )


def test_safe_policy_blocks_unbridged_role_relationship(tmp_path: Path):
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [
            _relationship_row(
                "relationship_candidate_000001",
                subject="role_entity_grandmother_of_people_group_000004",
            )
        ],
    )
    _write_viz(tmp_path, [_relationship_item("relationship_candidate_000001")])

    result = apply_review_suggestions(tmp_path, dry_run=True)

    assert result["suggestions_selected"] == 0
    reasons = [skip["reason"] for skip in result["skipped"]]
    assert any("role identity not bridged" in reason for reason in reasons)

    legacy = apply_review_suggestions(tmp_path, dry_run=True, policy="legacy")
    assert legacy["suggestions_selected"] == 1


def test_safe_policy_blocks_single_evidence_relationship_unless_high_confidence(tmp_path: Path):
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [
            _relationship_row("relationship_candidate_000001", evidence=1, confidence=0.8),
            _relationship_row("relationship_candidate_000002", evidence=1, confidence=0.9),
            _relationship_row("relationship_candidate_000003", evidence=2, confidence=0.8),
        ],
    )
    _write_viz(
        tmp_path,
        [
            _relationship_item("relationship_candidate_000001"),
            _relationship_item("relationship_candidate_000002"),
            _relationship_item("relationship_candidate_000003"),
        ],
    )

    result = apply_review_suggestions(tmp_path, dry_run=True)

    selected_targets = {action["target_id"] for action in result["selected_actions"]}
    assert selected_targets == {"relationship_candidate_000002", "relationship_candidate_000003"}
    reasons = [skip["reason"] for skip in result["skipped"]]
    assert any("single evidence mention" in reason for reason in reasons)


def test_safe_policy_blocks_contradicted_relationship(tmp_path: Path):
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [_relationship_row("relationship_candidate_000001", contradicting=True)],
    )
    _write_viz(tmp_path, [_relationship_item("relationship_candidate_000001")])

    result = apply_review_suggestions(tmp_path, dry_run=True)
    assert result["suggestions_selected"] == 0
    assert any("contradicting evidence" in skip["reason"] for skip in result["skipped"])


def test_safe_policy_confidence_floor_per_action(tmp_path: Path):
    _write_jsonl(
        tmp_path / "face_identity_candidates.jsonl",
        [
            {
                "id": "face_identity_candidate_000001",
                "face_cluster_id": "face_cluster_000001",
                "person_group_id": "people_group_000001",
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "face_clusters.jsonl",
        [{"id": "face_cluster_000001", "review_status": "needs_review"}],
    )
    _write_viz(
        tmp_path,
        [
            _item(
                "item_low",
                "face_identity_candidate_000001",
                "face_identity_candidate",
                "confirm_identity",
                {"face_cluster_id": "face_cluster_000001", "person_group_id": "people_group_000001"},
                confidence=0.6,  # below the 0.75 confirm_identity floor
            )
        ],
    )

    result = apply_review_suggestions(tmp_path, dry_run=True)
    assert result["suggestions_selected"] == 0
    assert any("below confirm_identity floor" in skip["reason"] for skip in result["skipped"])

    legacy = apply_review_suggestions(tmp_path, dry_run=True, policy="legacy")
    assert legacy["suggestions_selected"] == 1


def test_safe_policy_applies_well_supported_actions(tmp_path: Path):
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [_relationship_row("relationship_candidate_000001")],
    )
    _write_viz(tmp_path, [_relationship_item("relationship_candidate_000001")])

    result = apply_review_suggestions(tmp_path, reviewer="auto-pipeline")

    assert result["actions_applied"] == 1
    rows = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert rows[0]["review_status"] == "confirmed"
    corrections = read_jsonl(tmp_path / "corrections.jsonl")
    assert corrections[0]["reviewer"] == "auto-pipeline"


def test_unknown_policy_rejected(tmp_path: Path):
    _write_viz(tmp_path, [])
    with pytest.raises(ValueError, match="policy must be one of"):
        apply_review_suggestions(tmp_path, dry_run=True, policy="yolo")

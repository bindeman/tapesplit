"""Closed-loop calibration: review outcomes tune the safe-policy floors."""

import json
from pathlib import Path

from tapesplit.calibration import (
    analyze_review_outcomes,
    calibrate_review_policy,
    load_review_policy_floors,
)
from tapesplit.review_actions import SAFE_AUTO_ACCEPT_MIN_CONFIDENCE, apply_review_suggestions


def _write_corrections(project: Path, rows: list[dict]) -> None:
    project.mkdir(exist_ok=True)
    (project / "corrections.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def _correction(action: str, target: str, reviewer: str) -> dict:
    return {"action": action, "target_id": target, "reviewer": reviewer}


def test_outcome_classification(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    _write_corrections(
        project,
        [
            _correction("confirm_identity", "fic_1", "phillip"),
            _correction("confirm_identity", "fic_2", "auto-pipeline"),
            _correction("reject_identity", "fic_2", "phillip"),  # human walked back auto
            _correction("reject_identity", "fic_3", "phillip"),  # plain human rejection
            _correction("confirm_relationship", "rel_1", "auto-pipeline"),
        ],
    )

    outcomes = analyze_review_outcomes(project)

    assert outcomes["confirm_identity"]["human_confirmed"] == 1
    assert outcomes["confirm_identity"]["auto_accepted"] == 1
    assert outcomes["confirm_identity"]["auto_overridden"] == 1
    assert outcomes["confirm_identity"]["human_rejected"] == 1
    assert outcomes["confirm_relationship"]["auto_accepted"] == 1


def test_calibration_raises_floor_on_poor_precision(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    rows = []
    # 8 human rejections of speaker identities, 2 confirmations → low precision.
    for index in range(8):
        rows.append(_correction("reject_speaker_identity", f"sic_{index}", "phillip"))
    for index in range(2):
        rows.append(_correction("confirm_speaker_identity", f"sic_ok_{index}", "phillip"))
    _write_corrections(project, rows)

    policy = calibrate_review_policy(project)

    base = SAFE_AUTO_ACCEPT_MIN_CONFIDENCE["confirm_speaker_identity"]
    tuned = policy["min_confidence_by_action"]["confirm_speaker_identity"]
    assert tuned > base
    assert policy["report"]["confirm_speaker_identity"]["adjustment"] == "raised"
    assert (project / "review_policy.json").exists()

    floors = load_review_policy_floors(project)
    assert floors["confirm_speaker_identity"] == tuned


def test_calibration_lowers_floor_on_strong_precision(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    rows = [_correction("confirm_place_context", f"edge_{index}", "phillip") for index in range(12)]
    _write_corrections(project, rows)

    policy = calibrate_review_policy(project)
    base = SAFE_AUTO_ACCEPT_MIN_CONFIDENCE["confirm_place_context"]
    tuned = policy["min_confidence_by_action"]["confirm_place_context"]
    assert tuned < base
    assert policy["report"]["confirm_place_context"]["adjustment"] == "lowered"


def test_calibration_keeps_base_without_data(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    _write_corrections(project, [_correction("confirm_person", "pg_1", "phillip")])

    policy = calibrate_review_policy(project, write=False)
    report = policy["report"]["confirm_person"]
    assert report["adjustment"] == "insufficient-data"
    assert report["tuned_floor"] == SAFE_AUTO_ACCEPT_MIN_CONFIDENCE["confirm_person"]
    assert not (project / "review_policy.json").exists()


def test_apply_suggestions_uses_tuned_floor(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    (project / "face_identity_candidates.jsonl").write_text(
        json.dumps(
            {
                "id": "fic_1",
                "face_cluster_id": "fc_1",
                "person_group_id": "pg_1",
                "review_status": "needs_review",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "face_clusters.jsonl").write_text(
        json.dumps({"id": "fc_1", "review_status": "needs_review"}) + "\n", encoding="utf-8"
    )
    item = {
        "id": "item_1",
        "source_id": "fic_1",
        "source_record_type": "face_identity_candidate",
        "review_status": "needs_review",
        "confidence": 0.8,
        "suggested_action": {
            "action": "confirm_identity",
            "target_id": "fic_1",
            "target_type": "face_identity_candidate",
            "label": "Appears to be Filip",
            "confidence": 0.8,  # above the 0.75 base floor
            "payload": {"face_cluster_id": "fc_1", "person_group_id": "pg_1"},
        },
    }
    (project / "visualization.json").write_text(
        json.dumps({"review_queue": [item], "review_backlog": []}) + "\n", encoding="utf-8"
    )

    # Without a policy file the suggestion clears the 0.75 base floor.
    result = apply_review_suggestions(project, dry_run=True)
    assert result["suggestions_selected"] == 1

    # A tuned policy raising the floor above 0.8 blocks the same suggestion.
    (project / "review_policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "min_confidence_by_action": {"confirm_identity": 0.9},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = apply_review_suggestions(project, dry_run=True)
    assert result["suggestions_selected"] == 0
    assert any("floor 0.9" in skip["reason"] for skip in result["skipped"])

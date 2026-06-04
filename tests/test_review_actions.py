import json
from pathlib import Path

from tapesplit.cli import main
from tapesplit.review_actions import apply_review_actions
from tapesplit.storage import read_jsonl


def test_apply_review_actions_confirms_face_identity(tmp_path: Path):
    _write_jsonl(
        tmp_path / "face_identity_candidates.jsonl",
        [
            {
                "id": "face_identity_candidate_000001",
                "face_cluster_id": "face_cluster_000001",
                "person_group_id": "people_group_000001",
                "person_label": "Filip",
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "face_clusters.jsonl",
        [
            {
                "id": "face_cluster_000001",
                "candidate_people": [{"person_group_id": "people_group_000001"}],
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "face_observations.jsonl",
        [
            {
                "id": "face_observation_000001",
                "face_cluster_id": "face_cluster_000001",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [{"id": "people_group_000001", "label": "Filip", "review_status": "needs_review"}],
    )

    result = apply_review_actions(
        tmp_path,
        actions=[
            {
                "action": "confirm_identity",
                "target_id": "face_identity_candidate_000001",
                "reviewer": "test",
            }
        ],
    )

    candidates = read_jsonl(tmp_path / "face_identity_candidates.jsonl")
    clusters = read_jsonl(tmp_path / "face_clusters.jsonl")
    faces = read_jsonl(tmp_path / "face_observations.jsonl")
    people = read_jsonl(tmp_path / "people_groups.jsonl")
    corrections = read_jsonl(tmp_path / "corrections.jsonl")

    assert result["actions_applied"] == 1
    assert candidates[0]["review_status"] == "confirmed"
    assert clusters[0]["linked_person_group_id"] == "people_group_000001"
    assert clusters[0]["candidate_people"][0]["review_status"] == "confirmed"
    assert faces[0]["person_group_id"] == "people_group_000001"
    assert faces[0]["identity_review_status"] == "confirmed"
    assert people[0]["confirmed_face_cluster_ids"] == ["face_cluster_000001"]
    assert corrections[0]["action"] == "confirm_identity"


def test_apply_review_actions_scopes_one_home_without_merging_other_homes(tmp_path: Path):
    _write_jsonl(
        tmp_path / "place_groups.jsonl",
        [
            {
                "id": "place_group_000001",
                "label": "home",
                "kind": "generic_place_context",
                "scope_label": "Madison, Wisconsin context",
                "parent_place_labels": ["Madison, Wisconsin"],
                "canonical_event_ids": ["canonical_event_000001"],
                "review_status": "needs_review",
                "not_exportable_as_gps": True,
            },
            {
                "id": "place_group_000002",
                "label": "home",
                "kind": "generic_place_context",
                "scope_label": "Eugene, Oregon context",
                "parent_place_labels": ["Eugene, Oregon"],
                "canonical_event_ids": ["canonical_event_000002"],
                "review_status": "needs_review",
                "not_exportable_as_gps": True,
            },
        ],
    )

    apply_review_actions(
        tmp_path,
        actions=[
            {
                "action": "confirm_place",
                "target_id": "place_group_000001",
                "scope_label": "Madison home, 2001-2003",
                "parent_place_labels": ["Madison, Wisconsin"],
                "start_year": 2001,
                "end_year": 2003,
                "lat": 43.0731,
                "lng": -89.4012,
                "formatted_address": "Madison, WI, USA",
                "exportable_as_gps": True,
            }
        ],
    )

    places = read_jsonl(tmp_path / "place_groups.jsonl")
    madison_home = places[0]
    eugene_home = places[1]

    assert madison_home["review_status"] == "confirmed"
    assert madison_home["scope_label"] == "Madison home, 2001-2003"
    assert madison_home["not_exportable_as_gps"] is False
    assert madison_home["metadata"]["reviewed_scope"] == {
        "end_year": 2003,
        "start_year": 2001,
    }
    assert madison_home["metadata"]["selected_geocode"]["lat"] == 43.0731
    assert eugene_home["review_status"] == "needs_review"
    assert eugene_home["scope_label"] == "Eugene, Oregon context"
    assert eugene_home["not_exportable_as_gps"] is True


def test_apply_review_actions_merges_people_without_hardcoded_aliases(tmp_path: Path):
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Philip",
                "aliases": ["Phil"],
                "canonical_event_ids": ["canonical_event_000001"],
            },
            {
                "id": "people_group_000002",
                "label": "Filip",
                "aliases": ["Filya"],
                "canonical_event_ids": ["canonical_event_000002"],
            },
        ],
    )
    _write_jsonl(
        tmp_path / "face_observations.jsonl",
        [{"id": "face_observation_000001", "person_group_id": "people_group_000002"}],
    )
    _write_jsonl(
        tmp_path / "face_clusters.jsonl",
        [
            {
                "id": "face_cluster_000001",
                "candidate_people": [{"person_group_id": "people_group_000002", "person_label": "Filip"}],
            }
        ],
    )
    _write_jsonl(
        tmp_path / "face_identity_candidates.jsonl",
        [
            {
                "id": "face_identity_candidate_000001",
                "face_cluster_id": "face_cluster_000001",
                "person_group_id": "people_group_000002",
                "person_label": "Filip",
            }
        ],
    )

    apply_review_actions(
        tmp_path,
        actions=[
            {
                "action": "merge_person",
                "target_id": "people_group_000002",
                "merge_with_person_group_id": "people_group_000001",
            }
        ],
    )

    people = read_jsonl(tmp_path / "people_groups.jsonl")
    faces = read_jsonl(tmp_path / "face_observations.jsonl")
    clusters = read_jsonl(tmp_path / "face_clusters.jsonl")
    candidates = read_jsonl(tmp_path / "face_identity_candidates.jsonl")
    destination = people[0]
    source = people[1]

    assert destination["canonical_event_ids"] == ["canonical_event_000001", "canonical_event_000002"]
    assert destination["merged_person_group_ids"] == ["people_group_000002"]
    assert set(destination["aliases"]) == {"Phil", "Filip", "Filya"}
    assert source["review_status"] == "merged"
    assert source["merged_into_person_group_id"] == "people_group_000001"
    assert faces[0]["person_group_id"] == "people_group_000001"
    assert clusters[0]["candidate_people"][0]["person_group_id"] == "people_group_000001"
    assert candidates[0]["person_group_id"] == "people_group_000001"


def test_review_apply_cli_loads_jsonl_actions(tmp_path: Path):
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [
            {
                "id": "date_group_000001",
                "label": "Sep 7 2005",
                "date_value": "2005-09-07",
                "precision": "day",
                "review_status": "needs_review",
            }
        ],
    )
    actions_path = tmp_path / "actions.jsonl"
    _write_jsonl(
        actions_path,
        [{"action": "confirm_event_date", "target_id": "date_group_000001", "reviewer": "cli-test"}],
    )

    assert main(["review", "apply", str(tmp_path), str(actions_path)]) == 0
    dates = read_jsonl(tmp_path / "date_groups.jsonl")
    corrections = read_jsonl(tmp_path / "corrections.jsonl")

    assert dates[0]["review_status"] == "confirmed"
    assert dates[0]["reviewed_by"] == "cli-test"
    assert corrections[0]["action"] == "confirm_event_date"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

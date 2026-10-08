import json
from pathlib import Path

from tapesplit.cli import main
from tapesplit.review_actions import (
    apply_review_actions,
    apply_review_suggestions,
    list_review_corrections,
    reapply_review_corrections,
)
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


def test_apply_review_actions_confirms_speaker_identity(tmp_path: Path):
    _write_jsonl(
        tmp_path / "speaker_identity_candidates.jsonl",
        [
            {
                "id": "speaker_identity_candidate_000001",
                "speaker_label": "LOCAL_SPEAKER_00",
                "person_group_id": "people_group_000001",
                "person_label": "Ekaterina",
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "speaker_segments.jsonl",
        [{"id": "speaker_segment_000001", "speaker_label": "LOCAL_SPEAKER_00", "start_s": 1, "end_s": 2}],
    )
    _write_jsonl(tmp_path / "people_groups.jsonl", [{"id": "people_group_000001", "label": "Ekaterina"}])

    result = apply_review_actions(
        tmp_path,
        actions=[
            {
                "action": "confirm_speaker_identity",
                "target_id": "speaker_identity_candidate_000001",
                "payload": {"speaker_label": "LOCAL_SPEAKER_00", "person_group_id": "people_group_000001"},
            }
        ],
    )

    candidates = read_jsonl(tmp_path / "speaker_identity_candidates.jsonl")
    segments = read_jsonl(tmp_path / "speaker_segments.jsonl")
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert result["by_action"]["confirm_speaker_identity"] == 1
    assert candidates[0]["review_status"] == "confirmed"
    assert segments[0]["person_group_id"] == "people_group_000001"
    assert people[0]["confirmed_speaker_labels"] == ["LOCAL_SPEAKER_00"]


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


def test_reapply_review_corrections_restores_generated_people_changes(tmp_path: Path):
    original_people = [
        {
            "id": "people_group_000001",
            "label": "Ekaterina",
            "aliases": [],
            "canonical_event_ids": ["canonical_event_000001"],
        },
        {
            "id": "people_group_000002",
            "label": "Mom",
            "aliases": [],
            "canonical_event_ids": ["canonical_event_000002"],
            "kind": "role_candidate",
        },
    ]
    _write_jsonl(tmp_path / "people_groups.jsonl", original_people)

    apply_review_actions(
        tmp_path,
        actions=[
            {
                "action": "merge_person",
                "target_id": "people_group_000002",
                "merge_with_person_group_id": "people_group_000001",
                "notes": "Mom in this event is Ekaterina.",
            }
        ],
    )

    _write_jsonl(tmp_path / "people_groups.jsonl", original_people)
    result = reapply_review_corrections(tmp_path)
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert result["corrections_applied"] == 1
    assert result["corrections_skipped"] == 0
    assert people[0]["merged_person_group_ids"] == ["people_group_000002"]
    assert people[1]["merged_into_person_group_id"] == "people_group_000001"
    assert people[1]["review_status"] == "merged"


def test_reapply_review_corrections_skips_missing_generated_targets(tmp_path: Path):
    _write_jsonl(
        tmp_path / "corrections.jsonl",
        [
            {
                "id": "correction_000001",
                "action": "rename_event",
                "target_type": "event",
                "target_id": "canonical_event_999999",
                "reviewed_at": "2026-01-01T00:00:00+00:00",
                "reviewer": "test",
                "notes": "",
                "payload": {"title": "Corrected title"},
            }
        ],
    )

    result = reapply_review_corrections(tmp_path)

    assert result["corrections_applied"] == 0
    assert result["corrections_skipped"] == 1
    assert result["skipped"][0]["target_id"] == "canonical_event_999999"


def test_review_list_cli_summarizes_corrections(tmp_path: Path, capsys):
    _write_jsonl(
        tmp_path / "corrections.jsonl",
        [
            {
                "id": "correction_000001",
                "action": "confirm_place",
                "target_type": "place_group",
                "target_id": "place_group_000001",
                "reviewed_at": "2026-01-01T00:00:00+00:00",
                "reviewer": "test",
                "notes": "",
                "payload": {"label": "Madison home"},
            }
        ],
    )

    assert main(["review", "list", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    listed = list_review_corrections(tmp_path)

    assert output["corrections"] == 1
    assert output["by_action"] == {"confirm_place": 1}
    assert listed["items"][0]["target_id"] == "place_group_000001"


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


def test_apply_review_suggestions_applies_primary_only(tmp_path: Path):
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [
            {"id": "date_group_000001", "label": "Sep 7 2005", "review_status": "needs_review"},
            {"id": "date_group_000002", "label": "Sep 8 2005", "review_status": "needs_review"},
        ],
    )
    _write_visualization(
        tmp_path,
        review_queue=[
            _suggested_item(
                "review_item_000001",
                "date_group_000001",
                "date_group",
                "confirm_event_date",
                {"date_value": "2005-09-07", "precision": "day"},
            )
        ],
        review_backlog=[
            _suggested_item(
                "review_backlog_item_000001",
                "date_group_000002",
                "date_group",
                "confirm_event_date",
                {"date_value": "2005-09-08", "precision": "day"},
            )
        ],
    )

    result = apply_review_suggestions(tmp_path, tier="primary", reviewer="test")
    dates = read_jsonl(tmp_path / "date_groups.jsonl")
    corrections = read_jsonl(tmp_path / "corrections.jsonl")

    assert result["suggestions_selected"] == 1
    assert dates[0]["review_status"] == "confirmed"
    assert dates[0]["reviewed_by"] == "test"
    assert dates[1]["review_status"] == "needs_review"
    assert corrections[0]["source_action_id"] == "review_item_000001_confirm_event_date_01"


def test_apply_review_suggestions_dry_run_does_not_write(tmp_path: Path):
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [{"id": "date_group_000001", "label": "Sep 7 2005", "review_status": "needs_review"}],
    )
    _write_visualization(
        tmp_path,
        review_queue=[
            _suggested_item(
                "review_item_000001",
                "date_group_000001",
                "date_group",
                "confirm_event_date",
                {"date_value": "2005-09-07", "precision": "day"},
            )
        ],
    )

    result = apply_review_suggestions(tmp_path, dry_run=True)
    dates = read_jsonl(tmp_path / "date_groups.jsonl")

    assert result["suggestions_selected"] == 1
    assert result["actions_applied"] == 0
    assert result["selected_actions"][0]["target_id"] == "date_group_000001"
    assert dates[0]["review_status"] == "needs_review"
    assert not (tmp_path / "corrections.jsonl").exists()


def test_apply_review_suggestions_expands_grouped_relationships(tmp_path: Path):
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [
            {
                "id": "relationship_candidate_000001",
                "review_status": "needs_review",
                "subject_entity_id": "people_group_000001",
                "object_entity_id": "people_group_000002",
                "evidence_ids": ["ev_000001", "ev_000002"],
                "confidence": 0.8,
            },
            {
                "id": "relationship_candidate_000002",
                "review_status": "needs_review",
                "subject_entity_id": "people_group_000001",
                "object_entity_id": "people_group_000002",
                "evidence_ids": ["ev_000003", "ev_000004"],
                "confidence": 0.8,
            },
        ],
    )
    _write_visualization(
        tmp_path,
        review_queue=[
            _suggested_item(
                "review_item_000001",
                "relationship_candidate_000001",
                "relationship_candidate",
                "confirm_relationship",
                {
                    "predicate": "mother_of",
                    "subject_label": "Ekaterina",
                    "object_label": "Filip",
                    "relationship_ids": ["relationship_candidate_000001", "relationship_candidate_000002"],
                },
            )
        ],
    )

    result = apply_review_suggestions(tmp_path)
    relationships = read_jsonl(tmp_path / "relationship_candidates.jsonl")

    assert result["suggestions_selected"] == 2
    assert result["by_action"] == {"confirm_relationship": 2}
    assert [row["review_status"] for row in relationships] == ["confirmed", "confirmed"]


def test_apply_review_suggestions_confirms_speaker_identity(tmp_path: Path):
    _write_jsonl(
        tmp_path / "speaker_identity_candidates.jsonl",
        [
            {
                "id": "speaker_identity_candidate_000001",
                "speaker_label": "LOCAL_SPEAKER_00",
                "person_group_id": "people_group_000001",
                "person_label": "Ekaterina",
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "speaker_segments.jsonl",
        [{"id": "speaker_segment_000001", "speaker_label": "LOCAL_SPEAKER_00"}],
    )
    _write_jsonl(tmp_path / "people_groups.jsonl", [{"id": "people_group_000001", "label": "Ekaterina"}])
    _write_visualization(
        tmp_path,
        review_queue=[
            _suggested_item(
                "review_item_000001",
                "speaker_identity_candidate_000001",
                "speaker_identity_candidate",
                "confirm_speaker_identity",
                {"speaker_label": "LOCAL_SPEAKER_00", "person_group_id": "people_group_000001"},
            )
        ],
    )

    result = apply_review_suggestions(tmp_path)
    segments = read_jsonl(tmp_path / "speaker_segments.jsonl")
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert result["by_action"] == {"confirm_speaker_identity": 1}
    assert segments[0]["person_group_id"] == "people_group_000001"
    assert people[0]["confirmed_speaker_labels"] == ["LOCAL_SPEAKER_00"]


def test_apply_review_suggestions_skips_duplicate_targets(tmp_path: Path):
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [{"id": "date_group_000001", "label": "Sep 7 2005", "review_status": "needs_review"}],
    )
    _write_visualization(
        tmp_path,
        review_queue=[
            _suggested_item(
                "review_item_000001",
                "date_group_000001",
                "date_group",
                "confirm_event_date",
                {"date_value": "2005-09-07", "precision": "day"},
            ),
            _suggested_item(
                "review_item_000002",
                "date_group_000001",
                "date_group",
                "confirm_event_date",
                {"date_value": "2005-09-07", "precision": "day"},
            ),
        ],
    )

    result = apply_review_suggestions(tmp_path, dry_run=True)

    assert result["suggestions_selected"] == 1
    assert result["skipped"][0]["reason"] == "duplicate target"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _write_visualization(
    path: Path,
    *,
    review_queue: list[dict] | None = None,
    review_backlog: list[dict] | None = None,
) -> None:
    payload = {"review_queue": review_queue or [], "review_backlog": review_backlog or []}
    (path / "visualization.json").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _suggested_item(
    item_id: str,
    target_id: str,
    target_type: str,
    action: str,
    payload: dict,
) -> dict:
    return {
        "id": item_id,
        "source_id": target_id,
        "source_record_type": target_type,
        "review_status": "needs_review",
        "confidence": 0.9,
        "suggested_action": {
            "action": action,
            "target_id": target_id,
            "target_type": target_type,
            "label": "Best guess",
            "rationale": "Test suggestion",
            "confidence": 0.9,
            "payload": payload,
        },
    }


# ---------------------------------------------------------------------------- id drift
# Generated ids are sort ranks and renumber on every rebuild. Corrections carry a
# content fingerprint, and replay must follow it (or refuse), never the old number.


def _person(row_id: str, label: str, key: str, **extra) -> dict:
    return {"id": row_id, "label": label, "aliases": [], "metadata": {"normalized_key": key}, **extra}


def test_replay_follows_the_fingerprint_when_ids_renumber(tmp_path: Path):
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [_person("people_group_000001", "Andrei", "andrei"), _person("people_group_000002", "Elena", "elena")],
    )
    apply_review_actions(
        tmp_path,
        actions=[{"action": "confirm_person", "target_id": "people_group_000002", "label": "Mom", "reviewer": "test"}],
    )
    correction = read_jsonl(tmp_path / "corrections.jsonl")[0]
    assert correction["target_fingerprint"] == "person:elena"

    # A rebuild adds a person that sorts first: everyone's number moves.
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            _person("people_group_000001", "Alla", "ala"),
            _person("people_group_000002", "Andrei", "andrei"),
            _person("people_group_000003", "Elena", "elena"),
        ],
    )
    result = reapply_review_corrections(tmp_path)
    people = {row["id"]: row for row in read_jsonl(tmp_path / "people_groups.jsonl")}

    assert result["corrections_applied"] == 1
    assert people["people_group_000003"]["label"] == "Mom"
    assert people["people_group_000002"]["label"] == "Andrei"
    assert "review_status" not in people["people_group_000002"]


def test_replay_skips_a_fingerprint_that_matches_nothing(tmp_path: Path):
    _write_jsonl(tmp_path / "people_groups.jsonl", [_person("people_group_000001", "Elena", "elena")])
    apply_review_actions(
        tmp_path,
        actions=[{"action": "confirm_person", "target_id": "people_group_000001", "label": "Mom", "reviewer": "test"}],
    )
    _write_jsonl(tmp_path / "people_groups.jsonl", [_person("people_group_000001", "Fred", "fred")])

    result = reapply_review_corrections(tmp_path)
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert result["corrections_applied"] == 0
    assert result["corrections_skipped"] == 1
    assert people[0]["label"] == "Fred"


def test_merge_destination_follows_its_fingerprint(tmp_path: Path):
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [_person("people_group_000001", "Ekaterina", "ekaterina"), _person("people_group_000002", "Mom", "mom", kind="role_candidate")],
    )
    apply_review_actions(
        tmp_path,
        actions=[{"action": "merge_person", "target_id": "people_group_000002",
                  "merge_with_person_group_id": "people_group_000001", "reviewer": "test"}],
    )
    assert read_jsonl(tmp_path / "corrections.jsonl")[0]["ref_fingerprints"] == {"merge_with_person_group_id": "person:ekaterina"}

    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            _person("people_group_000001", "Andrei", "andrei"),
            _person("people_group_000002", "Ekaterina", "ekaterina"),
            _person("people_group_000003", "Mom", "mom", kind="role_candidate"),
        ],
    )
    reapply_review_corrections(tmp_path)
    people = {row["id"]: row for row in read_jsonl(tmp_path / "people_groups.jsonl")}

    assert people["people_group_000002"]["merged_person_group_ids"] == ["people_group_000003"]
    assert "merged_person_group_ids" not in people["people_group_000001"]


def test_role_groups_keep_their_fingerprint_when_events_renumber(tmp_path: Path):
    span = {"source_ranges": [{"source_video_id": "video_000018", "start_s": 4164.0, "end_s": 4486.1}]}
    _write_jsonl(tmp_path / "canonical_events.jsonl", [{"id": "canonical_event_000002", "metadata": span}])
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [_person("people_group_000001", "Mom", "role:mom:canonical_event_000002", kind="role_candidate")],
    )
    apply_review_actions(
        tmp_path,
        actions=[{"action": "mark_role_only", "target_id": "people_group_000001", "reviewer": "test"}],
    )
    assert read_jsonl(tmp_path / "corrections.jsonl")[0]["target_fingerprint"] == "person:role:mom:video_000018@4164-4486"

    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [{"id": "canonical_event_000002", "metadata": {"source_ranges": []}}, {"id": "canonical_event_000005", "metadata": span}],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            _person("people_group_000001", "Grandma", "role:grandma:canonical_event_000002", kind="role_candidate"),
            _person("people_group_000002", "Mom", "role:mom:canonical_event_000005", kind="role_candidate"),
        ],
    )
    result = reapply_review_corrections(tmp_path)
    people = {row["id"]: row for row in read_jsonl(tmp_path / "people_groups.jsonl")}

    assert result["corrections_applied"] == 1
    assert people["people_group_000002"]["metadata"]["identity_role_only"] is True
    assert "identity_role_only" not in people["people_group_000001"]["metadata"]


def test_quarantine_stops_replaying_id_only_machine_corrections(tmp_path: Path):
    from tapesplit.review_actions import quarantine_unfingerprinted_corrections

    rows = [
        {"id": "correction_000001", "action": "confirm_person", "target_id": "people_group_000001",
         "reviewer": "auto-pipeline", "payload": {}},
        {"id": "correction_000002", "action": "confirm_person", "target_id": "people_group_000001",
         "reviewer": "auto-pipeline", "target_fingerprint": "person:elena", "payload": {}},
        {"id": "correction_000003", "action": "confirm_person", "target_id": "people_group_000001",
         "reviewer": "phillip", "payload": {}},
    ]
    _write_jsonl(tmp_path / "corrections.jsonl", rows)

    first = quarantine_unfingerprinted_corrections(tmp_path)
    second = quarantine_unfingerprinted_corrections(tmp_path)
    stored = {row["id"]: row for row in read_jsonl(tmp_path / "corrections.jsonl")}

    assert first["quarantined"] == 1
    assert second["quarantined"] == 0
    assert stored["correction_000001"]["superseded_by"] == "id_drift_quarantine"
    assert not stored["correction_000002"].get("superseded")
    assert not stored["correction_000003"].get("superseded")


def test_apply_suggestions_does_not_reaccept_a_decision_already_on_file(tmp_path: Path):
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [{"id": "date_group_000002", "label": "Sep 7 2005", "metadata": {"normalized_key": "sep 7 2005"},
          "review_status": "needs_review"}],
    )
    _write_jsonl(
        tmp_path / "corrections.jsonl",
        [{"id": "correction_000001", "action": "confirm_event_date", "target_id": "date_group_000001",
          "target_fingerprint": "date:sep 7 2005", "reviewer": "auto-pipeline", "payload": {}}],
    )
    _write_visualization(
        tmp_path,
        review_queue=[
            _suggested_item("review_item_000001", "date_group_000002", "date_group", "confirm_event_date",
                            {"date_value": "2005-09-07", "precision": "day"}),
        ],
    )

    result = apply_review_suggestions(tmp_path, dry_run=True)

    assert result["suggestions_selected"] == 0
    assert any(item["reason"] == "already decided" for item in result["skipped"])

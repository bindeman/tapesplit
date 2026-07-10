"""Multi-signal identity fusion: name rules, fusion math, hard negatives."""

import json
from pathlib import Path

from tapesplit.identity_fusion import (
    BLOCKED_CONFIDENCE_CAP,
    DEFAULT_FUSION_WEIGHTS,
    SINGLE_DIMENSION_CONFIDENCE_CAP,
    _Dimension,
    _fuse,
    _name_pair_score,
    build_person_merge_candidates,
)
from tapesplit.visualization import _review_item_tier, _suggested_review_action


def test_name_pair_rules_relate_diminutives_and_scripts():
    score, reason = _name_pair_score("Ekaterina", "Katya")
    assert score >= 0.9, reason

    score, _ = _name_pair_score("Филя", "Filya")
    assert score >= 0.9

    score, _ = _name_pair_score("Philip", "Filipp")
    assert score >= 0.9

    score, _ = _name_pair_score("Tanya", "Татьяна")
    assert score >= 0.9


def test_name_pair_rules_reject_lookalikes_and_roles():
    assert _name_pair_score("Katya", "Lenin")[0] == 0.0
    assert _name_pair_score("Katya", "Glenn")[0] == 0.0
    assert _name_pair_score("Mom", "Mother")[0] == 0.0
    assert _name_pair_score("Grandma (First Day)", "Katya")[0] == 0.0


def test_fusion_two_dimensions_clear_the_merge_floor():
    result = _fuse(
        [
            _Dimension("name", 0.95, "diminutive pair"),
            _Dimension("face_cluster", 0.8, "shared cluster"),
        ],
        DEFAULT_FUSION_WEIGHTS,
    )
    assert result.confidence > 0.7
    assert not result.blocked


def test_fusion_single_dimension_is_capped_below_auto_accept():
    result = _fuse([_Dimension("name", 0.99, "identical")], DEFAULT_FUSION_WEIGHTS)
    assert result.confidence <= SINGLE_DIMENSION_CONFIDENCE_CAP


def test_fusion_hard_negative_blocks():
    result = _fuse(
        [
            _Dimension("name", 0.95, "diminutive pair"),
            _Dimension("face_cluster", 0.8, "shared cluster"),
            _Dimension("co_presence", 0.02, "both faces in frame scene_1", blocking=True),
        ],
        DEFAULT_FUSION_WEIGHTS,
    )
    assert result.blocked
    assert result.confidence <= BLOCKED_CONFIDENCE_CAP
    assert "frame" in result.blocked_reason


def test_stray_alias_matches_are_capped():
    from tapesplit.identity_fusion import UNCONFIRMED_ALIAS_SCORE_CAP, _name_dimension

    class _NoLabse:
        available = False

    filip_group = {
        "id": "pg_1",
        "label": "Filip / Filya",
        # "Ekaterina" accreted from co-mentions; it is NOT in the label.
        "aliases": ["Filip", "Filya", "Ekaterina"],
    }
    ekaterina_group = {"id": "pg_2", "label": "Ekaterina / Katya", "aliases": ["Ekaterina", "Katya"]}
    dimension = _name_dimension(filip_group, ekaterina_group, _NoLabse())
    assert dimension.score is not None
    assert dimension.score <= UNCONFIRMED_ALIAS_SCORE_CAP
    assert "capped" in dimension.detail


def test_name_disagreement_caps_contaminated_cluster_pairs(tmp_path: Path):
    """A shared cluster claiming two unrelated names must stay review-only."""

    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "people_groups.jsonl",
        [
            {
                "id": "pg_filip",
                "kind": "person",
                "label": "Filip / Filya",
                "aliases": ["Filip", "Filya", "Ekaterina"],  # stray Ekaterina alias
                "canonical_event_ids": ["ev_1", "ev_2"],
                "evidence_ids": [],
            },
            {
                "id": "pg_ekaterina",
                "kind": "person",
                "label": "Ekaterina / Katya",
                "aliases": ["Ekaterina", "Katya"],
                "canonical_event_ids": ["ev_2"],
                "evidence_ids": [],
            },
        ],
    )
    _write_jsonl(
        project / "face_identity_candidates.jsonl",
        [
            {"face_cluster_id": "fc_1", "person_group_id": "pg_filip", "confidence": 0.9},
            {"face_cluster_id": "fc_1", "person_group_id": "pg_ekaterina", "confidence": 0.9},
        ],
    )
    _write_jsonl(project / "face_clusters.jsonl", [])
    _write_jsonl(project / "face_observations.jsonl", [])
    _write_jsonl(project / "speaker_segments.jsonl", [])

    build_person_merge_candidates(project, use_labse=False, use_face_embeddings=False, use_voice=False)
    rows = [
        json.loads(line)
        for line in (project / "person_merge_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["confidence"] <= 0.65
    assert any(d["dimension"] == "name_guard" for d in rows[0]["dimensions"])


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8"
    )


def _project_with_duplicates(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "kind": "person",
                "label": "Ekaterina / Katya",
                "aliases": ["Ekaterina", "Katya"],
                "canonical_event_ids": ["ev_1", "ev_2", "ev_3"],
                "evidence_ids": ["evid_1"],
            },
            {
                "id": "people_group_000002",
                "kind": "person",
                "label": "Ekaterina / Katya / Катя",
                "aliases": ["Ekaterina", "Катя"],
                "canonical_event_ids": ["ev_2", "ev_4"],
                "evidence_ids": ["evid_2"],
            },
            {
                "id": "people_group_000003",
                "kind": "person",
                "label": "Glenn",
                "aliases": ["Glenn"],
                "canonical_event_ids": ["ev_9"],
                "evidence_ids": [],
            },
            {
                "id": "people_group_000004",
                "kind": "person",
                "label": "Boris",
                "aliases": ["Boris"],
                "canonical_event_ids": ["ev_1", "ev_2"],
                "evidence_ids": [],
            },
            {
                "id": "people_group_000005",
                "kind": "person",
                "label": "Borya",
                "aliases": ["Borya"],
                "canonical_event_ids": ["ev_3"],
                "evidence_ids": [],
            },
        ],
    )
    _write_jsonl(
        project / "face_identity_candidates.jsonl",
        [
            # Shared cluster candidacy for the Ekaterina pair.
            {"face_cluster_id": "fc_1", "person_group_id": "people_group_000001", "confidence": 0.7},
            {"face_cluster_id": "fc_1", "person_group_id": "people_group_000002", "confidence": 0.6},
            # Exclusive strong clusters for Boris and Borya (name-related pair).
            {"face_cluster_id": "fc_2", "person_group_id": "people_group_000004", "confidence": 0.8},
            {"face_cluster_id": "fc_3", "person_group_id": "people_group_000005", "confidence": 0.8},
        ],
    )
    _write_jsonl(project / "face_clusters.jsonl", [])
    _write_jsonl(
        project / "face_observations.jsonl",
        [
            # Boris and Borya appear together in two distinct frames -> hard negative.
            {"id": "obs_1", "face_cluster_id": "fc_2", "source_subject_id": "scene_1", "source_video_id": "v1", "time_s": 10.0},
            {"id": "obs_2", "face_cluster_id": "fc_3", "source_subject_id": "scene_1", "source_video_id": "v1", "time_s": 10.2},
            {"id": "obs_3", "face_cluster_id": "fc_2", "source_subject_id": "scene_2", "source_video_id": "v1", "time_s": 55.0},
            {"id": "obs_4", "face_cluster_id": "fc_3", "source_subject_id": "scene_2", "source_video_id": "v1", "time_s": 55.1},
        ],
    )
    _write_jsonl(project / "speaker_segments.jsonl", [])
    return project


def test_build_candidates_end_to_end(tmp_path: Path):
    project = _project_with_duplicates(tmp_path)
    summary = build_person_merge_candidates(
        project, use_labse=False, use_face_embeddings=False, use_voice=False
    )
    rows = [
        json.loads(line)
        for line in (project / "person_merge_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert summary["candidates"] == len(rows) > 0

    by_pair = {
        frozenset((row["person_group_id_a"], row["person_group_id_b"])): row for row in rows
    }
    ekaterina = by_pair[frozenset(("people_group_000001", "people_group_000002"))]
    assert ekaterina["confidence"] > 0.7
    assert not ekaterina["blocked"]
    assert {"name", "face_cluster"} <= set(ekaterina["fired_dimensions"])
    # The richer profile is the destination (side A).
    assert ekaterina["person_group_id_a"] == "people_group_000001"

    boris = by_pair[frozenset(("people_group_000004", "people_group_000005"))]
    assert boris["blocked"]
    assert boris["confidence"] <= BLOCKED_CONFIDENCE_CAP
    assert "frame" in boris["blocked_reason"]

    assert not any("people_group_000003" in pair for pair in by_pair)  # Glenn matched nothing


def test_weights_seam_reads_review_policy(tmp_path: Path):
    project = _project_with_duplicates(tmp_path)
    build_person_merge_candidates(project, use_labse=False, use_face_embeddings=False, use_voice=False)
    baseline = json.loads(
        (project / "person_merge_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )["confidence"]

    (project / "review_policy.json").write_text(
        json.dumps({"identity_fusion_weights": {"face_cluster": 0.1}}) + "\n", encoding="utf-8"
    )
    build_person_merge_candidates(project, use_labse=False, use_face_embeddings=False, use_voice=False)
    tuned = json.loads(
        (project / "person_merge_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )["confidence"]
    assert tuned < baseline


def test_review_item_suggestion_and_tier():
    item = {
        "task_type": "resolve_duplicate_person",
        "confidence": 0.86,
        "candidate": {
            "person_merge_candidate_id": "person_merge_candidate_000001",
            "person_group_id_a": "people_group_000001",
            "person_group_id_b": "people_group_000002",
            "label_a": "Ekaterina / Katya",
            "label_b": "Ekaterina / Katya / Катя",
            "blocked": False,
            "fired_dimensions": ["name", "face_cluster"],
            "dimensions": [],
        },
    }
    suggestion = _suggested_review_action(item)
    assert suggestion["action"] == "merge_person"
    assert suggestion["target_id"] == "people_group_000002"
    assert suggestion["payload"]["merge_with_person_group_id"] == "people_group_000001"
    tier, _reason = _review_item_tier(item)
    assert tier == "primary"

    blocked_item = {
        "task_type": "resolve_duplicate_person",
        "confidence": 0.2,
        "candidate": {**item["candidate"], "blocked": True},
    }
    assert _suggested_review_action(blocked_item) == {}
    tier, reason = _review_item_tier(blocked_item)
    assert tier == "backlog"
    assert "contradicting" in reason.casefold()

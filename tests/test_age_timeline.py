"""Age dimension: birth-year posteriors, age-anchor claims, fusion trajectory term."""

import json
from pathlib import Path

import pytest

from tapesplit.age_timeline import build_person_age_models, load_person_age_models
from tapesplit.identity_fusion import _age_trajectory_dimension


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


def _project_with_age_data(tmp_path: Path) -> Path:
    """A child filmed at ages ~4 (1998) and ~10 (2004): birth year ~1994."""

    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "tapes.jsonl",
        [
            {"id": "video_000001", "path": str(tmp_path / "a.mp4"), "probe": {"duration_s": 1000.0}},
            {"id": "video_000002", "path": str(tmp_path / "b.mp4"), "probe": {"duration_s": 1000.0}},
            {"id": "video_000003", "path": str(tmp_path / "c.mp4"), "probe": {"duration_s": 1000.0}},
        ],
    )
    _write_jsonl(
        project / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 0.0,
                "end_s": 900.0,
            },
            {
                "id": "canonical_event_000002",
                "source_video_id": "video_000002",
                "start_s": 0.0,
                "end_s": 900.0,
            },
        ],
    )
    _write_jsonl(
        project / "date_groups.jsonl",
        [
            {
                "id": "date_group_000001",
                "date_value": "1998-06-01",
                "excluded_as_event_date": False,
                "canonical_event_ids": ["canonical_event_000001"],
            },
            {
                "id": "date_group_000002",
                "date_value": "2004-06-01",
                "excluded_as_event_date": False,
                "canonical_event_ids": ["canonical_event_000002"],
            },
            # Narrated-historical dates must never become footage years.
            {
                "id": "date_group_000003",
                "date_value": "1912-01-01",
                "excluded_as_event_date": True,
                "canonical_event_ids": ["canonical_event_000001"],
            },
        ],
    )
    _write_jsonl(
        project / "face_clusters.jsonl",
        [
            {"id": "face_cluster_000001", "linked_person_group_id": "people_group_000001"},
            {"id": "face_cluster_000002", "linked_person_group_id": "people_group_000001"},
        ],
    )
    _write_jsonl(project / "face_identity_candidates.jsonl", [])
    observations = []
    embeddings = []
    for index, (cluster, media, age) in enumerate(
        [
            ("face_cluster_000001", "video_000001", 4.0),
            ("face_cluster_000001", "video_000001", 4.5),
            ("face_cluster_000002", "video_000002", 10.0),
            ("face_cluster_000002", "video_000002", 9.5),
        ],
        start=1,
    ):
        observation_id = f"face_observation_{index:06d}"
        observations.append(
            {
                "id": observation_id,
                "source_video_id": media,
                "time_s": 100.0 * index,
                "face_cluster_id": cluster,
                "face_quality_weight": 0.8,
            }
        )
        embeddings.append({"id": observation_id, "age_raw": age})
    _write_jsonl(project / "face_observations.jsonl", observations)
    _write_jsonl(project / "face_embeddings.jsonl", embeddings)
    return project


def test_birth_year_posterior_spans_eras(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TAPESPLIT_CLAIMS", "0")
    project = _project_with_age_data(tmp_path)
    summary = build_person_age_models(project)
    assert summary["people_modeled"] == 1
    models = load_person_age_models(project)
    model = models["people_group_000001"]
    assert model["sample_count"] == 4
    assert model["birth_year"] == pytest.approx(1994.0, abs=1.0)
    assert model["sigma"] >= 1.5
    assert not model["adult"]
    assert model["attribution_basis"] == "confirmed"
    assert set(model["observed_years"]) == {1998, 2004}


def test_track_ages_preferred_over_member_observations(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TAPESPLIT_CLAIMS", "0")
    project = _project_with_age_data(tmp_path)
    # A track covering observation 1 with a very different age; the member
    # observation must not double-count.
    _write_jsonl(
        project / "face_tracks.jsonl",
        [
            {
                "id": "track_000000000001",
                "media_id": "video_000001",
                "scene_id": "s1",
                "span": {"clock": "source", "start_s": 90.0, "end_s": 110.0},
                "frame_count": 8,
                "quality": {"mean_w": 0.7, "max_w": 0.8},
                "age_estimate": 4.2,
                "member_face_observation_ids": ["face_observation_000001"],
                "face_cluster_id": "face_cluster_000001",
                "mean_det_score": 0.8,
            }
        ],
    )
    build_person_age_models(project)
    model = load_person_age_models(project)["people_group_000001"]
    assert model["sample_count"] == 4  # track replaces its member, others unchanged
    bases = {sample["basis"] for sample in model["samples"]}
    assert "track:track_000000000001" in bases
    assert "observation:face_observation_000001" not in bases


def test_age_anchor_claims_only_for_undated_media(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TAPESPLIT_CLAIMS", "1")
    project = _project_with_age_data(tmp_path)
    _write_jsonl(
        project / "face_tracks.jsonl",
        [
            # Dated media: no age_anchor claim.
            {
                "id": "track_00000000000a",
                "media_id": "video_000001",
                "scene_id": "s1",
                "span": {"clock": "source", "start_s": 90.0, "end_s": 110.0},
                "frame_count": 8,
                "quality": {"mean_w": 0.7, "max_w": 0.8},
                "age_estimate": 4.2,
                "member_face_observation_ids": [],
                "face_cluster_id": "face_cluster_000001",
                "mean_det_score": 0.8,
            },
            # Undated media with an attributed, modeled person: anchored.
            {
                "id": "track_00000000000b",
                "media_id": "video_000003",
                "scene_id": "s3",
                "span": {"clock": "source", "start_s": 10.0, "end_s": 20.0},
                "frame_count": 6,
                "quality": {"mean_w": 0.7, "max_w": 0.8},
                "age_estimate": 7.0,
                "member_face_observation_ids": [],
                "face_cluster_id": "face_cluster_000001",
                "mean_det_score": 0.8,
            },
        ],
    )
    summary = build_person_age_models(project)
    assert summary["claims_error"] is None
    assert summary["attribute_claims"] == 2
    assert summary["age_anchor_claims"] == 1

    import sqlite3

    with sqlite3.connect(project / "claim_store.sqlite3") as connection:
        rows = connection.execute(
            "SELECT media_id, payload FROM claims WHERE kind='date' AND superseded_by IS NULL"
        ).fetchall()
    assert len(rows) == 1
    media_id, payload_json = rows[0]
    assertion = json.loads(payload_json)["assertion"]
    assert media_id == "video_000003"
    assert assertion["origin"] == "age_anchor"
    # birth ~1994 + apparent age 7 => capture ~2001
    assert assertion["year"] == pytest.approx(2001.0, abs=1.5)


def _model(person_id, birth_year, sigma=2.0, samples=4, years=(1998, 2004), age=6.0):
    return {
        "person_group_id": person_id,
        "birth_year": birth_year,
        "sigma": sigma,
        "sample_count": samples,
        "median_apparent_age": age,
        "adult": age >= 18,
        "observed_years": list(years),
    }


def test_fusion_age_trajectory_scores_and_veto():
    group_a = {"id": "people_group_000001"}
    group_b = {"id": "people_group_000002"}

    consistent = _age_trajectory_dimension(
        group_a,
        group_b,
        {
            "people_group_000001": _model("people_group_000001", 1994.2),
            "people_group_000002": _model("people_group_000002", 1994.9),
        },
    )
    assert consistent.score is not None and consistent.score > 0.85
    assert not consistent.blocking

    child_vs_adult = _age_trajectory_dimension(
        group_a,
        group_b,
        {
            "people_group_000001": _model("people_group_000001", 1994.0, sigma=2.0),
            "people_group_000002": _model(
                "people_group_000002", 1968.0, sigma=2.5, age=30.0, years=(1998, 2004)
            ),
        },
    )
    assert child_vs_adult.blocking
    assert child_vs_adult.score == pytest.approx(0.02)

    thin = _age_trajectory_dimension(
        group_a,
        group_b,
        {
            "people_group_000001": _model("people_group_000001", 1994.0, samples=1),
            "people_group_000002": _model("people_group_000002", 1994.0),
        },
    )
    assert thin.score is None

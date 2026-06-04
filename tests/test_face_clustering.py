import json
from pathlib import Path

from tapesplit.face_clustering import cluster_faces_for_project
from tapesplit.storage import read_jsonl


def test_cluster_faces_groups_similar_faces_and_proposes_people_from_event_context(
    tmp_path: Path,
    monkeypatch,
):
    _write_jsonl(
        tmp_path / "face_observations.jsonl",
        [
            {
                "id": "face_observation_000001",
                "source_video_id": "video_000001",
                "source_subject_type": "scene",
                "source_subject_id": "scene_1",
                "time_s": 5,
                "face_thumbnail_path": "thumbnails/faces/face_1.jpg",
            },
            {
                "id": "face_observation_000002",
                "source_video_id": "video_000001",
                "source_subject_type": "scene",
                "source_subject_id": "scene_2",
                "time_s": 6,
                "face_thumbnail_path": "thumbnails/faces/face_2.jpg",
            },
            {
                "id": "face_observation_000003",
                "source_video_id": "video_000001",
                "source_subject_type": "scene",
                "source_subject_id": "scene_3",
                "time_s": 50,
                "face_thumbnail_path": "thumbnails/faces/face_3.jpg",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 10,
                "relatedness": "likely_family",
            },
            {
                "id": "canonical_event_000002",
                "title": "School",
                "source_video_id": "video_000001",
                "start_s": 40,
                "end_s": 60,
                "relatedness": "likely_family",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip",
                "canonical_event_ids": ["canonical_event_000001"],
            },
            {
                "id": "people_group_000002",
                "label": "Emily",
                "canonical_event_ids": ["canonical_event_000002"],
            },
        ],
    )
    features = {
        "face_observation_000001": [1.0, 0.0],
        "face_observation_000002": [0.99, 0.01],
        "face_observation_000003": [0.0, 1.0],
    }
    monkeypatch.setattr(
        "tapesplit.face_clustering._face_feature",
        lambda _project, face: features[face["id"]],
    )

    result = cluster_faces_for_project(tmp_path, max_distance=0.05)
    faces = read_jsonl(tmp_path / "face_observations.jsonl")
    clusters = read_jsonl(tmp_path / "face_clusters.jsonl")
    candidates = read_jsonl(tmp_path / "face_identity_candidates.jsonl")

    assert result["face_clusters"] == 2
    assert faces[0]["face_cluster_id"] == faces[1]["face_cluster_id"]
    assert faces[2]["face_cluster_id"] != faces[0]["face_cluster_id"]
    assert clusters[0]["face_observation_ids"] == [
        "face_observation_000001",
        "face_observation_000002",
    ]
    assert clusters[0]["candidate_people"][0]["person_group_id"] == "people_group_000001"
    assert clusters[1]["candidate_people"][0]["person_label"] == "Emily"
    assert candidates[0]["face_cluster_id"] == "face_cluster_000001"
    assert candidates[0]["review_status"] == "needs_review"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

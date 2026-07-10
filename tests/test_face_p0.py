"""Face stack P0: quality weighting, constrained HAC, anchors, succession, deinterlace."""

import json
from pathlib import Path

import pytest

from tapesplit.face_clustering import (
    FACE_CLUSTER_SEED_MIN_WEIGHT,
    _assign_cluster_ids,
    _cluster_feature_rows,
    _CVLFaceKPRPEEmbedder,
    _resolve_face_embedding_backend,
    cluster_faces_for_project,
)
from tapesplit.face_embeddings import ensure_face_embeddings, load_face_embeddings
from tapesplit.face_quality import _quality_weight
from tapesplit.review_actions import (
    annotate_face_corrections_with_anchors,
    apply_review_actions,
    reapply_review_corrections,
)
from tapesplit.storage import read_jsonl
from tapesplit.visual_assets import _frame_extract_command


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _row(face_id, vector, *, weight=1.0, frame=None, video="video_000001", time_s=5.0, bbox=None):
    return {
        "face": {
            "id": face_id,
            "source_video_id": video,
            "source_subject_type": "scene",
            "source_subject_id": frame or f"scene_{face_id}",
            "time_s": time_s,
            "bbox": bbox or {"x": 10, "y": 10, "width": 50, "height": 50},
        },
        "feature": vector,
        "weight": weight,
        "frame_key": f"scene:{frame}" if frame else None,
        "seed": weight >= FACE_CLUSTER_SEED_MIN_WEIGHT,
    }


def test_quality_weight_is_continuous_and_eye_free():
    tiny = _quality_weight({"min_side": 30, "sharpness": 50, "brightness": 120})
    mid = _quality_weight({"min_side": 70, "sharpness": 50, "brightness": 120})
    full = _quality_weight({"min_side": 112, "sharpness": 60, "brightness": 120})
    assert 0.0 <= tiny < mid < full <= 1.0
    dark = _quality_weight({"min_side": 112, "sharpness": 60, "brightness": 5})
    assert dark < full
    blurry = _quality_weight({"min_side": 112, "sharpness": 1, "brightness": 120})
    assert 0.0 < blurry < full  # blur dampens, never zeroes


def test_hac_merges_similar_and_blocks_same_frame_pairs():
    rows = [
        _row("f1", [1.0, 0.0], frame="kf_1"),
        _row("f2", [0.999, 0.01], frame="kf_1"),  # same keyframe: cannot merge with f1
        _row("f3", [0.998, 0.02], frame="kf_2"),
        _row("f4", [0.0, 1.0], frame="kf_3"),
    ]
    result = _cluster_feature_rows(rows, max_distance=0.1)
    clusters = result["clusters"]
    by_face = {face["id"]: index for index, cluster in enumerate(clusters) for face in cluster["faces"]}
    assert by_face["f1"] != by_face["f2"]
    assert by_face["f3"] in (by_face["f1"], by_face["f2"])  # f3 joins one side
    assert by_face["f4"] not in (by_face["f1"], by_face["f2"])
    assert result["cannot_link_merges_blocked"] >= 1


def test_low_weight_faces_join_but_never_seed():
    rows = [
        _row("seed1", [1.0, 0.0], frame="kf_1"),
        _row("seed2", [0.999, 0.01], frame="kf_2"),
        _row("join1", [0.998, 0.02], weight=0.05, frame="kf_3"),  # near the cluster: joins
        _row("lone", [0.0, 1.0], weight=0.05, frame="kf_4"),  # far from everything: no cluster
    ]
    result = _cluster_feature_rows(rows, max_distance=0.1)
    clusters = result["clusters"]
    assert len(clusters) == 1
    ids = {face["id"] for face in clusters[0]["faces"]}
    assert ids == {"seed1", "seed2", "join1"}
    assert result["joined_without_seeding"] == 1


def test_succession_inherits_ids_and_flags_split_human_clusters():
    def face(face_id, time_s, video="video_000001"):
        return {
            "id": face_id,
            "source_video_id": video,
            "time_s": time_s,
            "bbox": {"x": 10, "y": 10, "width": 40, "height": 40},
        }

    previous_observations = {
        "fo_1": face("fo_1", 1.0),
        "fo_2": face("fo_2", 2.0),
        "fo_3": face("fo_3", 3.0),
        "fo_4": face("fo_4", 4.0),
    }
    previous_clusters = [
        {
            "id": "face_cluster_000001",
            "face_observation_ids": ["fo_1", "fo_2"],
            "label": "Filip",
            "linked_person_group_id": "pg_1",
            "review_status": "confirmed",
        },
        {"id": "face_cluster_000002", "face_observation_ids": ["fo_3"], "label": "Face cluster 2"},
    ]

    # Same members, new run (ids renumbered by re-detection but anchors match).
    clusters = [
        {"faces": [face("new_1", 1.0), face("new_2", 2.0)]},
        {"faces": [face("new_3", 3.0)]},
        {"faces": [face("new_4", 4.0)]},
    ]
    result = _assign_cluster_ids(
        clusters, previous_clusters=previous_clusters, previous_observations=previous_observations
    )
    assert clusters[0]["cluster_id"] == "face_cluster_000001"
    assert clusters[0]["inherited_fields"]["label"] == "Filip"
    assert clusters[0]["inherited_fields"]["linked_person_group_id"] == "pg_1"
    assert clusters[1]["cluster_id"] == "face_cluster_000002"
    assert clusters[2]["cluster_id"] == "face_cluster_000003"  # fresh id after max
    assert result["inherited"] == 2
    assert result["created"] == 1
    assert result["conflicts"] == []

    # Human-labeled cluster scattered across two big clusters -> loud conflict.
    scattered = [
        {"faces": [face("n1", 1.0), face("n5", 10.0), face("n6", 11.0), face("n7", 12.0)]},
        {"faces": [face("n2", 2.0), face("n8", 13.0), face("n9", 14.0), face("n10", 15.0)]},
    ]
    result = _assign_cluster_ids(
        scattered, previous_clusters=previous_clusters, previous_observations=previous_observations
    )
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["previous_cluster_id"] == "face_cluster_000001"
    assert conflict["label"] == "Filip"
    flagged = next(cluster for cluster in scattered if cluster.get("inherited_fields"))
    assert flagged["inherited_fields"]["review_status"] == "needs_review"
    assert any("split during" in note for note in flagged["inherited_fields"]["notes"])


def test_embeddings_persist_and_reuse(tmp_path: Path):
    project = tmp_path
    crop = project / "thumbnails" / "faces" / "f1.jpg"
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b"fake-jpeg-bytes")
    faces = [
        {
            "id": "fo_1",
            "source_video_id": "video_000001",
            "time_s": 5.0,
            "bbox": {"x": 1, "y": 2, "width": 30, "height": 30},
            "face_thumbnail_path": "thumbnails/faces/f1.jpg",
        }
    ]
    calls = []

    def describe(face):
        calls.append(face["id"])
        return {"vector": [0.6, 0.8], "det_score": 0.9, "age_raw": 7, "pose": {"yaw": 3.0}}

    first = ensure_face_embeddings(project, faces, feature_model="test_model", describe=describe)
    assert first["summary"] == {"computed": 1, "reused": 0, "failed": 0}
    assert calls == ["fo_1"]

    second = ensure_face_embeddings(project, faces, feature_model="test_model", describe=describe)
    assert second["summary"] == {"computed": 0, "reused": 1, "failed": 0}
    assert calls == ["fo_1"]  # cache hit: describe not called again

    loaded = load_face_embeddings(project)
    assert loaded["fo_1"]["age_raw"] == 7
    assert loaded["fo_1"]["span"]["clock"] == "source"
    assert loaded["fo_1"]["vector"] == pytest.approx([0.6, 0.8])

    crop.write_bytes(b"different-bytes")  # content changed -> recompute
    third = ensure_face_embeddings(project, faces, feature_model="test_model", describe=describe)
    assert third["summary"]["computed"] == 1
    assert calls == ["fo_1", "fo_1"]


def _anchored_project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "face_observations.jsonl",
        [
            {
                "id": "fo_1",
                "source_video_id": "video_000001",
                "time_s": 10.0,
                "bbox": {"x": 100, "y": 50, "width": 60, "height": 60},
                "face_cluster_id": "fc_1",
            },
            {
                "id": "fo_2",
                "source_video_id": "video_000001",
                "time_s": 20.0,
                "bbox": {"x": 300, "y": 80, "width": 50, "height": 50},
                "face_cluster_id": "fc_1",
            },
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
    _write_jsonl(project / "people_groups.jsonl", [{"id": "pg_1", "label": "Filip"}])
    return project


def test_face_actions_gain_anchors_and_replay_after_renumbering(tmp_path: Path):
    project = _anchored_project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "label_face_cluster",
                "target_id": "fc_1",
                "target_type": "face_cluster",
                "reviewer": "phillip",
                "payload": {"label": "Filip", "person_group_id": "pg_1"},
            }
        ],
    )
    corrections = read_jsonl(project / "corrections.jsonl")
    anchors = corrections[0]["payload"]["anchors"]
    assert len(anchors) == 2
    assert anchors[0]["media_id"] == "video_000001"
    assert anchors[0]["span"]["clock"] == "source"

    # Simulate a re-detection + re-cluster: every id renumbers, bboxes jitter.
    _write_jsonl(
        project / "face_observations.jsonl",
        [
            {
                "id": "fo_901",
                "source_video_id": "video_000001",
                "time_s": 10.2,
                "bbox": {"x": 104, "y": 53, "width": 58, "height": 58},
                "face_cluster_id": "fc_77",
            },
            {
                "id": "fo_902",
                "source_video_id": "video_000001",
                "time_s": 19.8,
                "bbox": {"x": 296, "y": 82, "width": 52, "height": 52},
                "face_cluster_id": "fc_77",
            },
        ],
    )
    _write_jsonl(
        project / "face_clusters.jsonl",
        [
            {
                "id": "fc_77",
                "face_observation_ids": ["fo_901", "fo_902"],
                "face_count": 2,
                "representative_face_observation_id": "fo_901",
                "review_status": "needs_review",
            }
        ],
    )

    result = reapply_review_corrections(project)
    assert result["corrections_applied"] == 1
    assert result["corrections_skipped"] == 0
    cluster = read_jsonl(project / "face_clusters.jsonl")[0]
    assert cluster["label"] == "Filip"
    assert cluster["linked_person_group_id"] == "pg_1"


def test_annotate_face_corrections_with_anchors_is_idempotent(tmp_path: Path):
    project = _anchored_project(tmp_path)
    _write_jsonl(
        project / "corrections.jsonl",
        [
            {
                "id": "correction_000001",
                "action": "label_face_cluster",
                "target_type": "face_cluster",
                "target_id": "fc_1",
                "reviewer": "auto-pipeline",
                "reviewed_at": "2026-07-01T00:00:00+00:00",
                "payload": {"label": "Filip"},
            }
        ],
    )
    first = annotate_face_corrections_with_anchors(project)
    assert first["annotated"] == 1
    corrections = read_jsonl(project / "corrections.jsonl")
    assert len(corrections[0]["payload"]["anchors"]) == 2
    second = annotate_face_corrections_with_anchors(project)
    assert second["annotated"] == 0


def test_frame_extraction_deinterlaces_before_scaling(tmp_path: Path):
    cmd = _frame_extract_command(tmp_path / "tape.mp4", 12.0, tmp_path / "frame.jpg", width=640)
    filters = cmd[cmd.index("-vf") + 1]
    assert filters == "bwdif=mode=send_frame,scale=640:-2"


def test_cvlface_backend_is_gated_when_weights_absent(monkeypatch):
    assert _resolve_face_embedding_backend("cvlface-kprpe") == "cvlface-kprpe"
    monkeypatch.setattr("tapesplit.face_clustering._cvlface_available", lambda: False)
    embedder = _CVLFaceKPRPEEmbedder()
    with pytest.raises(RuntimeError, match="local Hugging Face cache"):
        embedder._load()


def test_recluster_preserves_ids_end_to_end(tmp_path: Path, monkeypatch):
    _write_jsonl(
        tmp_path / "face_observations.jsonl",
        [
            {
                "id": "face_observation_000001",
                "source_video_id": "video_000001",
                "source_subject_type": "scene",
                "source_subject_id": "scene_1",
                "time_s": 5,
                "bbox": {"x": 10, "y": 10, "width": 40, "height": 40},
                "face_thumbnail_path": "thumbnails/faces/face_1.jpg",
            },
            {
                "id": "face_observation_000002",
                "source_video_id": "video_000001",
                "source_subject_type": "scene",
                "source_subject_id": "scene_2",
                "time_s": 6,
                "bbox": {"x": 12, "y": 11, "width": 42, "height": 41},
                "face_thumbnail_path": "thumbnails/faces/face_2.jpg",
            },
        ],
    )
    _write_jsonl(tmp_path / "canonical_events.jsonl", [])
    _write_jsonl(tmp_path / "people_groups.jsonl", [])
    features = {
        "face_observation_000001": [1.0, 0.0],
        "face_observation_000002": [0.99, 0.01],
    }
    monkeypatch.setattr(
        "tapesplit.face_clustering._face_feature",
        lambda _project, face: features[face["id"]],
    )
    monkeypatch.setattr(
        "tapesplit.face_clustering.analyze_face_quality",
        lambda _path: {"usable": True, "status": "usable", "quality_weight": 0.8, "notes": []},
    )

    first = cluster_faces_for_project(tmp_path, max_distance=0.1, embedding_backend="opencv-gray")
    assert first["face_clusters"] == 1
    assert first["clusters_new"] == 1

    # Human links the cluster to a person, then a re-cluster runs.
    clusters = read_jsonl(tmp_path / "face_clusters.jsonl")
    clusters[0]["linked_person_group_id"] = "pg_1"
    clusters[0]["review_status"] = "confirmed"
    _write_jsonl(tmp_path / "face_clusters.jsonl", clusters)

    second = cluster_faces_for_project(tmp_path, max_distance=0.1, embedding_backend="opencv-gray")
    assert second["clusters_inherited"] == 1
    assert second["clusters_new"] == 0
    assert second["succession_conflicts"] == []
    cluster = read_jsonl(tmp_path / "face_clusters.jsonl")[0]
    assert cluster["id"] == "face_cluster_000001"
    assert cluster["linked_person_group_id"] == "pg_1"
    assert cluster["review_status"] == "confirmed"

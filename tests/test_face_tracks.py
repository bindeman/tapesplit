"""Face tracks: association, aggregation, cannot-links, member joins, claims."""

import json
from pathlib import Path

import pytest

from tapesplit.face_clustering import (
    _assign_cluster_ids,
    _cluster_feature_rows,
    _cluster_record,
)
from tapesplit.face_tracks import (
    _associate,
    _bbox_iou,
    _dedupe_tracks,
    _expire_tracks,
    _finalize_track,
    _join_member_observations,
    _mark_co_occurrence,
    _weighted_mean_normalized,
    build_face_tracks_for_project,
)


def _bbox(x, y, size=80):
    return {"x": float(x), "y": float(y), "width": float(size), "height": float(size)}


def _detection(x, y, vector, det_score=0.8, size=80):
    return {
        "bbox": _bbox(x, y, size),
        "vector": vector,
        "det_score": det_score,
        "age_raw": 7.0,
        "gender_raw": "M",
    }


VEC_A = [1.0, 0.0, 0.0]
VEC_B = [0.0, 1.0, 0.0]


def _image():
    # numpy is an optional dependency (vision extra); tests that build a frame skip without it.
    np = pytest.importorskip("numpy")
    return np.zeros((480, 720, 3), dtype=np.uint8)


def test_association_tracks_two_people_without_switching():
    open_tracks, closed = [], []
    image = _image()
    # Two people, both drifting right; embeddings stay distinct.
    for frame in range(4):
        t = frame * 0.2
        detections = [
            _detection(100 + frame * 10, 100, VEC_A),
            _detection(400 + frame * 10, 300, VEC_B),
        ]
        _associate(open_tracks, closed, detections, frame_time=t, image=image)
        _expire_tracks(open_tracks, closed, now_s=t)
    assert len(open_tracks) == 2 and not closed
    lengths = sorted(len(track["samples"]) for track in open_tracks)
    assert lengths == [4, 4]
    # Each track's samples stay on one side of the frame — no identity switch.
    for track in open_tracks:
        xs = [sample["bbox"]["x"] for sample in track["samples"]]
        assert max(xs) - min(xs) < 200


def test_association_gap_expires_track():
    open_tracks, closed = [], []
    image = _image()
    _associate(open_tracks, closed, [_detection(100, 100, VEC_A)], frame_time=0.0, image=image)
    _expire_tracks(open_tracks, closed, now_s=5.0)  # > TRACK_MAX_GAP_S
    assert not open_tracks and len(closed) == 1


def test_weak_iou_requires_embedding_agreement():
    open_tracks, closed = [], []
    image = _image()
    _associate(open_tracks, closed, [_detection(100, 100, VEC_A)], frame_time=0.0, image=image)
    # Barely-overlapping detection with a very different embedding: new track.
    _associate(open_tracks, closed, [_detection(160, 160, VEC_B)], frame_time=0.2, image=image)
    assert len(open_tracks) == 2


def test_weighted_mean_normalized():
    result = _weighted_mean_normalized([[2.0, 0.0], [0.0, 2.0]], [3.0, 1.0])
    assert result is not None
    assert abs(sum(value * value for value in result) - 1.0) < 1e-9
    assert result[0] > result[1] > 0


def test_finalize_track_and_co_occurrence(tmp_path: Path):
    pytest.importorskip("cv2")  # _finalize_track writes the track thumbnails with OpenCV
    open_tracks, closed = [], []
    image = _image()
    for frame in range(3):
        t = frame * 0.2
        _associate(
            open_tracks,
            closed,
            [_detection(100, 100, VEC_A), _detection(400, 300, VEC_B)],
            frame_time=t,
            image=image,
        )
    scene = {"id": "video_000001_scene_000001", "source_video_id": "video_000001"}
    tracks = [_finalize_track(tmp_path, state, scene=scene) for state in open_tracks]
    tracks = [track for track in tracks if track]
    assert len(tracks) == 2
    _mark_co_occurrence(tracks)
    assert tracks[0]["co_occurring_track_ids"] == [tracks[1]["id"]]
    assert tracks[1]["co_occurring_track_ids"] == [tracks[0]["id"]]
    for track in tracks:
        assert track["id"].startswith("track_")
        assert track["frame_count"] == 3
        assert track["age_estimate"] == 7.0
        assert (tmp_path / track["representative"]["thumbnail_path"]).exists()
    # Content-hashed ids are deterministic for the same media/scene/geometry.
    rebuilt = _finalize_track(tmp_path, open_tracks[0], scene=scene)
    assert rebuilt["id"] == tracks[0]["id"]


def test_member_join_by_time_and_rescaled_iou():
    track = {
        "span": {"clock": "source", "start_s": 10.0, "end_s": 12.0},
        "frame_samples": [
            {"t": 10.0, "bbox": _bbox(112, 112, 90)},
            {"t": 11.0, "bbox": _bbox(120, 112, 90)},
        ],
    }
    tape = {"probe": {"width": 720}}
    observations = [
        # 640-wide keyframe coords: x=100 scales to 112.5 native — overlaps.
        {"id": "face_observation_000001", "time_s": 10.2, "bbox": _bbox(100, 100, 80)},
        # Right time, wrong place.
        {"id": "face_observation_000002", "time_s": 10.2, "bbox": _bbox(500, 300, 80)},
        # Right place, outside the span tolerance.
        {"id": "face_observation_000003", "time_s": 20.0, "bbox": _bbox(100, 100, 80)},
    ]
    members = _join_member_observations(track, observations, tape=tape)
    assert members == ["face_observation_000001"]


def test_cooccurring_tracks_never_cluster_together():
    pytest.importorskip("numpy")  # the constrained clustering math runs on numpy
    track_a = {
        "id": "track_aaa",
        "co_occurring_track_ids": ["track_bbb"],
        "quality": {"max_w": 0.8},
    }
    track_b = {
        "id": "track_bbb",
        "co_occurring_track_ids": ["track_aaa"],
        "quality": {"max_w": 0.8},
    }
    rows = []
    for track in (track_a, track_b):
        rows.append(
            {
                "face": None,
                "members": [],
                "track": track,
                "feature": [1.0, 0.0],  # identical vectors: would merge without the constraint
                "weight": 0.8,
                "frame_keys": {
                    "cooc:" + ":".join(sorted([track["id"], other]))
                    for other in track["co_occurring_track_ids"]
                },
                "seed": True,
            }
        )
    result = _cluster_feature_rows(rows, max_distance=0.65)
    assert len(result["clusters"]) == 2
    assert result["cannot_link_merges_blocked"] >= 1


def test_cluster_record_prefers_track_representative():
    track = {
        "id": "track_ccc",
        "media_id": "video_000001",
        "span": {"clock": "source", "start_s": 5.0, "end_s": 9.0},
        "frame_count": 12,
        "quality": {"max_w": 0.9, "mean_w": 0.7},
        "representative": {"t": 6.0, "bbox": _bbox(10, 10), "thumbnail_path": "thumbnails/tracks/track_ccc.jpg"},
    }
    face = {
        "id": "face_observation_000009",
        "source_video_id": "video_000001",
        "time_s": 6.0,
        "face_thumbnail_path": "thumbnails/faces/blurry.jpg",
        "face_quality_weight": 0.2,
        "face_quality_status": "low_quality",
    }
    record = _cluster_record(
        "face_cluster_000010",
        [face],
        tracks=[track],
        max_distance=0.65,
        candidate_people=[],
        method="m",
        feature_model="f",
    )
    assert record["thumbnail_path"] == "thumbnails/tracks/track_ccc.jpg"
    assert record["face_track_ids"] == ["track_ccc"]
    assert record["track_frame_count"] == 12
    assert record["first_start_s"] == 5.0
    assert record["last_end_s"] == 9.0


def test_succession_via_stable_track_ids():
    previous_clusters = [
        {
            "id": "face_cluster_000007",
            "label": "Filip",
            "linked_person_group_id": "people_group_000021",
            "face_observation_ids": [],
            "face_track_ids": ["track_stable_1", "track_stable_2"],
        }
    ]
    clusters = [
        {
            "faces": [],
            "tracks": [{"id": "track_stable_1"}, {"id": "track_stable_2"}],
        }
    ]
    result = _assign_cluster_ids(
        clusters, previous_clusters=previous_clusters, previous_observations={}
    )
    assert result["inherited"] == 1
    assert clusters[0]["cluster_id"] == "face_cluster_000007"
    assert clusters[0]["inherited_fields"]["linked_person_group_id"] == "people_group_000021"


def test_build_resumes_completed_scenes_without_decoding(tmp_path: Path):
    pytest.importorskip("numpy")  # the final consolidate step writes the track-vector file with numpy
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    (project / "tapes.jsonl").write_text(
        json.dumps(
            {"id": "video_000001", "path": str(tmp_path / "missing.mp4"), "probe": {"duration_s": 100.0, "width": 720}}
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "scenes.jsonl").write_text(
        json.dumps(
            {
                "id": "video_000001_scene_000001",
                "source_video_id": "video_000001",
                "scene_type": "content",
                "start_s": 1.0,
                "end_s": 4.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "face_observations.jsonl").write_text(
        json.dumps(
            {
                "id": "face_observation_000001",
                "source_subject_type": "scene",
                "source_subject_id": "video_000001_scene_000001",
                "source_video_id": "video_000001",
                "time_s": 2.0,
                "bbox": {"x": 10, "y": 10, "width": 60, "height": 60},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    track_row = {
        "kind": "track",
        "scene_id": "video_000001_scene_000001",
        "id": "track_resumed01",
        "media_id": "video_000001",
        "span": {"clock": "source", "start_s": 1.2, "end_s": 3.8},
        "frame_count": 5,
        "frame_samples": [],
        "top_frames": [],
        "representative": {"t": 2.0, "bbox": {}, "thumbnail_path": ""},
        "quality": {"mean_w": 0.5, "max_w": 0.6, "n_effective": 2.5},
        "member_face_observation_ids": ["face_observation_000001"],
        "co_occurring_track_ids": [],
        "face_cluster_id": "",
        "feature_model": "m",
    }
    partial = project / "face_tracks.partial.jsonl"
    partial.write_text(
        json.dumps({"kind": "scene_done", "scene_id": "video_000001_scene_000001"})
        + "\n"
        + json.dumps(track_row)
        + "\n",
        encoding="utf-8",
    )

    # The only scene is checkpointed, so no decode/detection happens even
    # though the source file does not exist and insightface may be absent.
    summary = build_face_tracks_for_project(project)
    assert summary["scenes_resumed"] == 1
    assert summary["scenes_processed"] == 0
    assert summary["tracks"] == 1
    rows = [json.loads(line) for line in (project / "face_tracks.jsonl").read_text().splitlines()]
    assert rows[0]["id"] == "track_resumed01"
    assert not partial.exists()


def test_dedupe_tracks_keeps_last_by_id():
    rows = [
        {"id": "track_x", "media_id": "a", "span": {"start_s": 1.0}},
        {"id": "track_x", "media_id": "a", "span": {"start_s": 1.0}, "face_cluster_id": "fc"},
    ]
    deduped = _dedupe_tracks(rows)
    assert len(deduped) == 1
    assert deduped[0]["face_cluster_id"] == "fc"


def test_resume_survives_truncated_checkpoint_tail(tmp_path: Path):
    pytest.importorskip("numpy")  # the final consolidate step writes the track-vector file with numpy
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    (project / "tapes.jsonl").write_text(
        json.dumps(
            {"id": "video_000001", "path": str(tmp_path / "missing.mp4"), "probe": {"duration_s": 100.0}}
        )
        + "\n",
        encoding="utf-8",
    )
    scenes = [
        {
            "id": f"video_000001_scene_{index:06d}",
            "source_video_id": "video_000001",
            "scene_type": "content",
            "start_s": float(index),
            "end_s": float(index) + 0.5,
        }
        for index in (1, 2)
    ]
    (project / "scenes.jsonl").write_text(
        "".join(json.dumps(scene) + "\n" for scene in scenes), encoding="utf-8"
    )
    (project / "face_observations.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "id": f"face_observation_{index:06d}",
                    "source_subject_type": "scene",
                    "source_subject_id": scene["id"],
                    "source_video_id": "video_000001",
                    "time_s": scene["start_s"],
                    "bbox": {"x": 1, "y": 1, "width": 50, "height": 50},
                }
            )
            + "\n"
            for index, scene in enumerate(scenes, start=1)
        ),
        encoding="utf-8",
    )
    track_row = {
        "kind": "track",
        "scene_id": scenes[0]["id"],
        "id": "track_survivor0001",
        "media_id": "video_000001",
        "span": {"clock": "source", "start_s": 1.0, "end_s": 1.4},
        "frame_count": 3,
        "frame_samples": [],
        "top_frames": [],
        "representative": {"t": 1.2, "bbox": {}, "thumbnail_path": ""},
        "quality": {"mean_w": 0.5, "max_w": 0.6, "n_effective": 1.5},
        "member_face_observation_ids": [],
        "co_occurring_track_ids": [],
        "face_cluster_id": "",
        "feature_model": "m",
    }
    # Scene 1 fully committed (rows then marker); scene 2's checkpoint was
    # killed mid-write: an orphan track row with no marker, truncated mid-line.
    partial = project / "face_tracks.partial.jsonl"
    partial.write_text(
        json.dumps(track_row)
        + "\n"
        + json.dumps({"kind": "scene_done", "scene_id": scenes[0]["id"]})
        + "\n"
        + '{"kind": "track", "scene_id": "video_000001_scene_000002", "id": "track_lost", "span": {"clock": "sou',
        encoding="utf-8",
    )

    # The malformed tail must not raise; scene 2 has no marker so it is not
    # "done" (here its source file is missing, so it skips at the tape check),
    # and the orphan unmarked track row never reaches the output.
    summary = build_face_tracks_for_project(project)
    assert summary["scenes_resumed"] == 1
    rows = [json.loads(line) for line in (project / "face_tracks.jsonl").read_text().splitlines()]
    assert [row["id"] for row in rows] == ["track_survivor0001"]


def test_bbox_iou():
    assert _bbox_iou(_bbox(0, 0, 100), _bbox(0, 0, 100)) == pytest.approx(1.0)
    assert _bbox_iou(_bbox(0, 0, 100), _bbox(200, 200, 100)) == 0.0
    assert 0.0 < _bbox_iou(_bbox(0, 0, 100), _bbox(50, 0, 100)) < 0.5

import json
from pathlib import Path

import pytest

from tapesplit.faces import detect_face_thumbnails_for_project, _vision_rect_to_pixel_bbox
from tapesplit.storage import read_jsonl
from tapesplit.visual_assets import extract_visual_assets_for_project


def test_extract_visual_assets_writes_scene_and_event_thumbnails(tmp_path: Path, monkeypatch):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [
            {
                "id": "video_000001",
                "filename": "tape.mp4",
                "path": str(tmp_path / "tape.mp4"),
                "probe": {"duration_s": 30},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "scenes.jsonl",
        [
            {
                "id": "video_000001_scene_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 10,
                "scene_type": "content",
                "label": "content",
            },
            {
                "id": "video_000001_scene_000002",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 20,
                "scene_type": "non_content",
                "label": "blank_black",
                "relatedness": "non_content",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 1,
                "end_s": 9,
                "title": "Birthday",
                "relatedness": "likely_family",
            }
        ],
    )

    def fake_extract_frame(_video_path: Path, _time_s: float, output_path: Path, *, width: int) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"fake-{width}".encode("utf-8"))

    monkeypatch.setattr("tapesplit.visual_assets._extract_frame", fake_extract_frame)

    result = extract_visual_assets_for_project(tmp_path, force=True)
    assets = read_jsonl(tmp_path / "visual_assets.jsonl")

    assert result["assets"] == 2
    assert [(asset["subject_type"], asset["subject_id"]) for asset in assets] == [
        ("scene", "video_000001_scene_000001"),
        ("event", "canonical_event_000001"),
    ]
    for asset in assets:
        assert (tmp_path / asset["keyframe_path"]).exists()
        assert (tmp_path / asset["thumbnail_path"]).exists()


def test_detect_face_thumbnails_writes_observations_from_visual_assets(tmp_path: Path, monkeypatch):
    pytest.importorskip("cv2")  # face quality scoring of each crop needs OpenCV (vision extra)
    keyframe = tmp_path / "keyframes" / "scenes" / "scene_1.jpg"
    keyframe.parent.mkdir(parents=True)
    keyframe.write_bytes(b"fake image")
    _write_jsonl(
        tmp_path / "visual_assets.jsonl",
        [
            {
                "id": "visual_asset_000001",
                "subject_type": "scene",
                "subject_id": "scene_1",
                "source_video_id": "video_000001",
                "start_s": 1,
                "end_s": 9,
                "time_s": 5,
                "keyframe_path": "keyframes/scenes/scene_1.jpg",
                "thumbnail_path": "thumbnails/scenes/scene_1.jpg",
            }
        ],
    )

    monkeypatch.setattr(
        "tapesplit.faces._detect_faces_in_image",
        lambda _path, *, min_size, backend: [{"bbox": {"x": 10, "y": 12, "width": 30, "height": 34}}],
    )

    def fake_crop(_image_path: Path, output_path: Path, _bbox: dict, *, padding: float = 0.25) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"face")

    monkeypatch.setattr("tapesplit.faces._crop_face_thumbnail", fake_crop)

    result = detect_face_thumbnails_for_project(tmp_path, backend="opencv")
    faces = read_jsonl(tmp_path / "face_observations.jsonl")

    assert result["faces"] == 1
    assert result["resolved_backend"] == "opencv"
    assert faces[0]["source_subject_id"] == "scene_1"
    assert faces[0]["detection_backend"] == "opencv"
    assert faces[0]["bbox"]["width"] == 30
    assert (tmp_path / faces[0]["face_thumbnail_path"]).exists()


def test_vision_rect_to_pixel_bbox_flips_normalized_y_axis():
    bbox = _vision_rect_to_pixel_bbox(
        ((0.25, 0.20), (0.50, 0.25)),
        image_width=200,
        image_height=100,
    )

    assert bbox == {"x": 50, "y": 55, "width": 100, "height": 25}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_transcript_anchor_picks_matching_moment():
    from tapesplit.visual_assets import _transcript_anchor_time

    event = {
        "title": "Filip Loses a Tooth",
        "summary": "The loose tooth finally comes out during the walk.",
        "evidence_ids": [],
    }
    source_range = {"source_video_id": "video_000001", "start_s": 0.0, "end_s": 600.0}
    transcripts = [
        {"source_video_id": "video_000001", "start_s": 30.0, "end_s": 35.0, "text": "look at the trees on this trail"},
        {"source_video_id": "video_000001", "start_s": 410.0, "end_s": 416.0, "text": "the tooth is loose, wiggle it, the tooth came out"},
        {"source_video_id": "video_000001", "start_s": 500.0, "end_s": 505.0, "text": "time to head back to the car"},
    ]
    anchored = _transcript_anchor_time(event, source_range, transcripts, {})
    assert anchored is not None
    assert 410.0 <= anchored <= 416.0

    # No lexical overlap → falls back (returns None).
    event_ru = {"title": "Case with no overlap", "summary": "unrelated words entirely", "evidence_ids": []}
    assert _transcript_anchor_time(event_ru, source_range, transcripts[:1], {}) is None

    # Evidence quotes bridge languages.
    event_bridge = {
        "title": "Different language title",
        "summary": "",
        "evidence_ids": ["ev_1"],
    }
    evidence = {"ev_1": {"text": "зуб шатается совсем зуб выпал сейчас"}}
    transcripts_ru = [
        {"source_video_id": "video_000001", "start_s": 200.0, "end_s": 206.0, "text": "смотри как красиво здесь очень"},
        {"source_video_id": "video_000001", "start_s": 300.0, "end_s": 306.0, "text": "зуб шатается ой зуб выпал"},
    ]
    anchored = _transcript_anchor_time(event_bridge, source_range, transcripts_ru, evidence)
    assert anchored is not None and 300.0 <= anchored <= 306.0

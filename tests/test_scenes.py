import json
from pathlib import Path

from tapesplit.scenes import (
    build_scene_intervals,
    detect_scenes_for_project,
    _parse_showinfo_cut_times,
)
from tapesplit.search import build_search_index, query_search_index
from tapesplit.storage import read_jsonl


def test_parse_showinfo_cut_times_deduplicates_and_sorts():
    stderr = """
    [Parsed_showinfo_1 @ 0x1] n:   0 pts: 150000 pts_time:5.000 pos: 10
    [Parsed_showinfo_1 @ 0x1] n:   1 pts: 150000 pts_time:5.000 pos: 10
    [Parsed_showinfo_1 @ 0x1] n:   2 pts: 900000 pts_time:30.250 pos: 90
    """

    assert _parse_showinfo_cut_times(stderr) == [5.0, 30.25]


def test_build_scene_intervals_splits_visual_cuts_and_non_content():
    scenes = build_scene_intervals(
        source_video_id="video_000001",
        duration_s=100.0,
        visual_cut_times=[10.0, 30.0, 60.0],
        non_content_ranges=[
            {
                "source_video_id": "video_000001",
                "start_s": 30.0,
                "end_s": 40.0,
                "label": "blue_screen_no_signal",
                "confidence": 0.95,
            }
        ],
        min_scene_seconds=1.0,
    )

    assert [(row["scene_type"], row["label"], row["start_s"], row["end_s"]) for row in scenes] == [
        ("content", "content", 0.0, 10.0),
        ("content", "content", 10.0, 30.0),
        ("non_content", "blue_screen_no_signal", 30.0, 40.0),
        ("content", "content", 40.0, 60.0),
        ("content", "content", 60.0, 100.0),
    ]
    assert scenes[2]["relatedness"] == "non_content"
    assert scenes[2]["confidence"] == 0.95
    assert "non_content_start" in scenes[2]["start_boundary_reasons"]


def test_build_scene_intervals_merges_short_content_scenes():
    scenes = build_scene_intervals(
        source_video_id="video_000001",
        duration_s=20.0,
        visual_cut_times=[10.0, 10.4, 15.0],
        non_content_ranges=[],
        min_scene_seconds=1.0,
    )

    assert [(row["start_s"], row["end_s"]) for row in scenes] == [
        (0.0, 10.4),
        (10.4, 15.0),
        (15.0, 20.0),
    ]


def test_detect_scenes_for_project_writes_scenes_and_search_indexes_visible_content(
    tmp_path: Path,
    monkeypatch,
):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [
            {
                "id": "video_000001",
                "filename": "tape.mp4",
                "path": str(tmp_path / "tape.mp4"),
                "probe": {"duration_s": 50.0},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "non_content_ranges.jsonl",
        [
            {
                "source_video_id": "video_000001",
                "start_s": 20.0,
                "end_s": 25.0,
                "label": "blank_black",
                "confidence": 0.9,
            }
        ],
    )

    monkeypatch.setattr("tapesplit.scenes.detect_visual_cut_times", lambda *_args, **_kwargs: [10.0, 30.0])

    result = detect_scenes_for_project(tmp_path, threshold=0.3)
    scenes = read_jsonl(tmp_path / "scenes.jsonl")

    assert result["scenes"] == 5
    assert scenes[0]["id"] == "video_000001_scene_000001"
    assert scenes[2]["scene_type"] == "non_content"
    assert scenes[2]["relatedness"] == "non_content"

    search_result = build_search_index(tmp_path)
    assert search_result["by_type"] == {"scene": 4}
    assert query_search_index(tmp_path, "visual cut content", limit=10)["results"]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

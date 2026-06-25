import json

from tapesplit.gemini_adapter import (
    estimate_chunked_video_analysis,
    estimate_project_videos,
    estimate_video_token_units,
    parse_json_object,
    plan_video_chunks,
    _proxy_encoding_profile,
    usage_units_from_response,
)


def test_estimate_video_token_units_low_resolution():
    units = estimate_video_token_units(
        duration_s=10,
        fps=1,
        media_resolution="low",
        output_tokens=100,
    )

    assert units == {
        "input_text_tokens": 1200,
        "input_video_tokens": 660,
        "input_audio_tokens": 320,
        "output_tokens": 100,
    }


def test_plan_video_chunks_uses_overlap():
    chunks = plan_video_chunks(duration_s=100, chunk_seconds=40, overlap_seconds=10)

    assert [(chunk.start_s, chunk.end_s) for chunk in chunks] == [
        (0.0, 40.0),
        (30.0, 70.0),
        (60.0, 100.0),
    ]


def test_estimate_chunked_video_analysis_counts_prompt_per_chunk():
    estimate = estimate_chunked_video_analysis(
        duration_s=100,
        model="gemini-2.5-flash",
        fps=1,
        media_resolution="low",
        output_tokens_per_chunk=100,
        chunk_seconds=40,
        overlap_seconds=10,
    )

    assert estimate["chunks"] == 3
    assert estimate["analyzed_duration_s"] == 120
    assert estimate["units"]["input_text_tokens"] == 3600
    assert estimate["units"]["input_video_tokens"] == 7920
    assert estimate["units"]["input_audio_tokens"] == 3840
    assert estimate["units"]["output_tokens"] == 300


def test_estimate_project_videos_all_aggregates_units(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_DEFAULT_FPS", "1")
    monkeypatch.setenv("GEMINI_MEDIA_RESOLUTION", "low")
    project = tmp_path / "project.tapesplit"
    project.mkdir()
    rows = [
        {"id": "video_000001", "filename": "one.mp4", "probe": {"duration_s": 10}},
        {"id": "video_000002", "filename": "two.mp4", "probe": {"duration_s": 20}},
    ]
    (project / "tapes.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    estimate = estimate_project_videos(project, all_videos=True, output_tokens=100)

    assert estimate["videos"] == 2
    assert estimate["total_duration_s"] == 30
    assert estimate["units"] == {
        "input_text_tokens": 2400,
        "input_video_tokens": 1980,
        "input_audio_tokens": 960,
        "output_tokens": 200,
    }
    assert [row["source_video_id"] for row in estimate["results"]] == ["video_000001", "video_000002"]


def test_estimate_project_videos_filters_to_twelvelabs_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_DEFAULT_FPS", "1")
    monkeypatch.setenv("GEMINI_MEDIA_RESOLUTION", "low")
    project = tmp_path / "project.tapesplit"
    project.mkdir()
    rows = [
        {"id": "video_000001", "filename": "one.mp4", "probe": {"duration_s": 10}},
        {"id": "video_000002", "filename": "two.mp4", "probe": {"duration_s": 20}},
    ]
    (project / "tapes.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    (project / "twelvelabs_assets.jsonl").write_text(
        json.dumps({"source_video_id": "video_000002", "asset": {"asset_id": "asset_2"}}) + "\n",
        encoding="utf-8",
    )

    estimate = estimate_project_videos(
        project,
        all_videos=True,
        uploaded_to_twelvelabs_only=True,
        output_tokens=100,
    )

    assert estimate["videos"] == 1
    assert estimate["results"][0]["source_video_id"] == "video_000002"


def test_estimate_video_token_units_high_fps_scales_visual_tokens():
    low_1fps = estimate_video_token_units(
        duration_s=10,
        fps=1,
        media_resolution="low",
        output_tokens=0,
    )
    low_5fps = estimate_video_token_units(
        duration_s=10,
        fps=5,
        media_resolution="low",
        output_tokens=0,
    )

    assert low_5fps["input_video_tokens"] == low_1fps["input_video_tokens"] * 5
    assert low_5fps["input_audio_tokens"] == low_1fps["input_audio_tokens"]


def test_proxy_encoding_profile_targets_upload_budget():
    profile = _proxy_encoding_profile(
        duration_s=7200,
        max_upload_bytes=1_450_000_000,
        target_height=480,
        target_fps=12,
        audio_bitrate_kbps=96,
    )

    assert profile["height"] == 480
    assert profile["fps"] == 12
    assert profile["audio_bitrate_kbps"] == 96
    assert profile["video_bitrate_kbps"] > 1000


def test_usage_units_from_response_counts_modalities_and_thinking_tokens():
    response = {
        "usageMetadata": {
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 5,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 10},
                {"modality": "VIDEO", "tokenCount": 100},
                {"modality": "AUDIO", "tokenCount": 30},
            ],
        }
    }

    assert usage_units_from_response(response) == {
        "input_text_tokens": 10,
        "input_video_tokens": 100,
        "input_audio_tokens": 30,
        "input_image_tokens": 0,
        "output_tokens": 25,
    }


def test_parse_json_object_handles_fenced_json():
    assert parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}


def test_parse_json_object_repairs_bare_timestamp_values():
    payload = parse_json_object(
        """
        {
          "scene_candidates": [
            {"start_s": 2:25, "end_s": 5:58},
            {"start_s": 1:02:03, "end_s": 1:02:10}
          ],
          "date_candidates": [{"value": "4/5/99", "start_s": 10}]
        }
        """
    )

    assert payload["scene_candidates"] == [
        {"start_s": 145, "end_s": 358},
        {"start_s": 3723, "end_s": 3730},
    ]
    assert payload["date_candidates"][0]["start_s"] == 10

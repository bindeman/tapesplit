from tapesplit.gemini_adapter import (
    estimate_chunked_video_analysis,
    estimate_video_token_units,
    parse_json_object,
    plan_video_chunks,
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

from tapesplit.gemini_adapter import estimate_video_token_units, parse_json_object, usage_units_from_response


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

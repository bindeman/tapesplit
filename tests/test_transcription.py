import json
from pathlib import Path

import pytest

from tapesplit.storage import read_jsonl
from tapesplit.transcription import import_transcript, parse_transcript_file


def test_parse_openai_whisper_json_segments(tmp_path: Path):
    path = tmp_path / "whisper.json"
    path.write_text(
        json.dumps(
            {
                "language": "ru",
                "segments": [
                    {"start": 1.25, "end": 3.5, "text": " Привет мир ", "no_speech_prob": 0.1}
                ],
            }
        ),
        encoding="utf-8",
    )

    assert parse_transcript_file(path) == [
        {
            "start_s": 1.25,
            "end_s": 3.5,
            "text": "Привет мир",
            "language": "ru",
            "confidence": 0.9,
            "metadata": {
                "raw_segment": {"start": 1.25, "end": 3.5, "text": " Привет мир ", "no_speech_prob": 0.1}
            },
        }
    ]


def test_parse_whisper_cpp_json_offsets(tmp_path: Path):
    path = tmp_path / "cpp.json"
    path.write_text(
        json.dumps(
            {
                "result": {"language": "en"},
                "transcription": [
                    {
                        "offsets": {"from": 2000, "to": 4500},
                        "text": "Here we are at Maplewood School.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    segments = parse_transcript_file(path)

    assert segments[0]["start_s"] == 2.0
    assert segments[0]["end_s"] == 4.5
    assert segments[0]["language"] == "en"
    assert segments[0]["text"] == "Here we are at Maplewood School."


def test_parse_srt_transcript(tmp_path: Path):
    path = tmp_path / "clip.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:03,250\nHere we are in Hawaii.\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\nLava is flowing.\n",
        encoding="utf-8",
    )

    segments = parse_transcript_file(path, default_language="en")

    assert [(row["start_s"], row["end_s"], row["text"], row["language"]) for row in segments] == [
        (1.0, 3.25, "Here we are in Hawaii.", "en"),
        (4.0, 5.0, "Lava is flowing.", "en"),
    ]


def test_import_transcript_writes_source_time_segments(tmp_path: Path):
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_000001","path":"/tmp/source.mp4"}\n',
        encoding="utf-8",
    )
    transcript = tmp_path / "clip.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nSchool assembly\n",
        encoding="utf-8",
    )

    result = import_transcript(
        tmp_path,
        transcript,
        source_video_id="video_000001",
        language="en",
        offset_seconds=10,
    )

    rows = read_jsonl(tmp_path / "transcript_segments.jsonl")
    assert result["segments_written"] == 1
    assert rows[0]["id"] == "tr_000001"
    assert rows[0]["source_video_id"] == "video_000001"
    assert rows[0]["start_s"] == 11.0
    assert rows[0]["end_s"] == 12.0
    assert rows[0]["text"] == "School assembly"


def test_import_transcript_requires_force_for_existing_source(tmp_path: Path):
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_000001","path":"/tmp/source.mp4"}\n',
        encoding="utf-8",
    )
    transcript = tmp_path / "clip.srt"
    transcript.write_text("1\n00:00:01,000 --> 00:00:02,000\nOne\n", encoding="utf-8")
    import_transcript(tmp_path, transcript, source_video_id="video_000001")

    with pytest.raises(FileExistsError):
        import_transcript(tmp_path, transcript, source_video_id="video_000001")

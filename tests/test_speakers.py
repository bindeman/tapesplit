import json
from pathlib import Path

from tapesplit.speakers import import_speaker_segments, parse_speaker_file
from tapesplit.storage import read_jsonl


def test_parse_rttm_speaker_segments(tmp_path: Path):
    path = tmp_path / "speakers.rttm"
    path.write_text("SPEAKER video_000001 1 10.000 2.500 <NA> <NA> SPEAKER_00 <NA> <NA>\n", encoding="utf-8")

    segments = parse_speaker_file(path)

    assert segments[0]["start_s"] == 10.0
    assert segments[0]["end_s"] == 12.5
    assert segments[0]["speaker_label"] == "SPEAKER_00"


def test_import_speaker_segments_writes_jsonl(tmp_path: Path):
    _write_jsonl(tmp_path / "tapes.jsonl", [{"id": "video_000001", "path": "video.mp4"}])
    speaker_file = tmp_path / "speakers.json"
    speaker_file.write_text(
        json.dumps(
            [
                {
                    "start_s": 1,
                    "end_s": 3,
                    "speaker_label": "SPEAKER_00",
                    "confidence": 0.8,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = import_speaker_segments(
        tmp_path,
        speaker_file,
        source_video_id="video_000001",
        force=True,
    )
    rows = read_jsonl(tmp_path / "speaker_segments.jsonl")

    assert result["speaker_segments"] == 1
    assert rows[0]["speaker_label"] == "SPEAKER_00"
    assert rows[0]["source_video_id"] == "video_000001"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

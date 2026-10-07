import json
from pathlib import Path

import pytest

from tapesplit.speakers import (
    _canonicalize_cluster_labels,
    _cluster_voice_embeddings,
    _label_transcript_rows_from_speaker_anchors,
    _merge_adjacent_speaker_segments,
    _sample_transcript_rows_for_speaker_embeddings,
    import_speaker_segments,
    parse_speaker_file,
)
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


def test_cluster_voice_embeddings_groups_similar_vectors():
    pytest.importorskip("sklearn")  # voice clustering uses scikit-learn (pulled in by the local-ai extra)
    labels = _cluster_voice_embeddings(
        [
            [1.0, 0.0, 0.0],
            [0.98, 0.02, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.99, 0.01],
        ],
        distance_threshold=0.1,
    )

    canonical = _canonicalize_cluster_labels(labels)

    assert canonical[0] == canonical[1]
    assert canonical[2] == canonical[3]
    assert canonical[0] != canonical[2]


def test_merge_adjacent_speaker_segments_preserves_turn_changes():
    segments = [
        _speaker_segment("video_000001", 1.0, 2.0, "LOCAL_SPEAKER_00", "tr_000001"),
        _speaker_segment("video_000001", 2.5, 3.0, "LOCAL_SPEAKER_00", "tr_000002"),
        _speaker_segment("video_000001", 3.2, 4.0, "LOCAL_SPEAKER_01", "tr_000003"),
    ]

    merged = _merge_adjacent_speaker_segments(segments)

    assert len(merged) == 2
    assert merged[0]["start_s"] == 1.0
    assert merged[0]["end_s"] == 3.0
    assert merged[0]["speaker_label"] == "LOCAL_SPEAKER_00"
    assert merged[1]["speaker_label"] == "LOCAL_SPEAKER_01"


def test_sample_transcript_rows_keeps_longest_row_per_time_bucket():
    rows = [
        {"id": "short", "start_s": 1.0, "end_s": 1.5},
        {"id": "long", "start_s": 2.0, "end_s": 5.0},
        {"id": "next", "start_s": 12.0, "end_s": 13.0},
    ]

    sampled = _sample_transcript_rows_for_speaker_embeddings(rows, stride_s=10.0)

    assert [row["id"] for row in sampled] == ["long", "next"]


def test_label_transcript_rows_from_nearest_speaker_anchors(tmp_path: Path):
    rows = [
        {"id": "tr_1", "start_s": 0.0, "end_s": 1.0, "text": "hello"},
        {"id": "tr_2", "start_s": 7.0, "end_s": 8.0, "text": "there"},
        {"id": "tr_3", "start_s": 15.0, "end_s": 16.0, "text": "again"},
    ]
    anchors = [rows[0], rows[2]]

    labeled = _label_transcript_rows_from_speaker_anchors(
        rows,
        anchors,
        [0, 1],
        [0.9, 0.8],
        source_video_id="video_000001",
        model_name="test",
        run_id="run",
        audio_path=tmp_path / "audio.wav",
    )

    assert [row["speaker_label"] for row in labeled] == [
        "LOCAL_SPEAKER_00",
        "LOCAL_SPEAKER_00",
        "LOCAL_SPEAKER_01",
    ]
    assert labeled[1]["metadata"]["anchor_transcript_segment_id"] == "tr_1"


def _speaker_segment(source_video_id: str, start: float, end: float, speaker: str, transcript_id: str) -> dict:
    return {
        "source_video_id": source_video_id,
        "start_s": start,
        "end_s": end,
        "speaker_label": speaker,
        "confidence": 0.8,
        "provider": "transcript-embedding",
        "model": "test",
        "metadata": {"transcript_segment_id": transcript_id},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

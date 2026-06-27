import json
from pathlib import Path

from tapesplit.content_classification import build_content_classifications_for_project, classify_event_content
from tapesplit.storage import read_jsonl


def test_classify_event_content_uses_family_and_unrelated_signals():
    family = classify_event_content(
        {
            "title": "Filip Birthday Party",
            "summary": "Children and family gather at home.",
            "relatedness": "likely_family",
            "metadata": {"people": ["Filip", "Ekaterina"]},
        },
        visual_text=[],
        captions=[],
        faces=[{"face_quality_status": "usable"}],
        evidence=[],
    )
    unrelated = classify_event_content(
        {
            "title": "Movie Credits",
            "summary": "Producer and studio credits.",
            "relatedness": "likely_unrelated",
            "metadata": {},
        },
        visual_text=[{"text": "PRODUCER"}],
        captions=[],
        faces=[],
        evidence=[],
    )

    assert family["label"] == "likely_family"
    assert unrelated["label"] == "likely_unrelated"


def test_build_content_classifications_writes_event_rows(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 30,
                "title": "Movie Credits",
                "summary": "Producer credits",
                "relatedness": "likely_unrelated",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "visual_text_observations.jsonl",
        [
            {
                "id": "visual_text_000001",
                "source_subject_id": "canonical_event_000001",
                "text": "PRODUCER",
            }
        ],
    )

    result = build_content_classifications_for_project(tmp_path)
    rows = read_jsonl(tmp_path / "content_classifications.jsonl")

    assert result["content_classifications"] == 1
    assert rows[0]["label"] == "likely_unrelated"
    assert rows[0]["source_subject_id"] == "canonical_event_000001"


def test_build_content_classifications_ignores_stale_classification_evidence(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 30,
                "title": "Outdoor walk",
                "evidence_ids": ["evidence_000001"],
            }
        ],
    )
    _write_jsonl(
        tmp_path / "evidence.jsonl",
        [
            {
                "id": "evidence_000001",
                "kind": "content_classification",
                "text": "Movie Credits: producer studio television broadcast",
            }
        ],
    )

    result = build_content_classifications_for_project(tmp_path)
    rows = read_jsonl(tmp_path / "content_classifications.jsonl")

    assert result["content_classifications"] == 1
    assert rows[0]["label"] == "uncertain"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

from pathlib import Path

from tapesplit.evidence import build_evidence


def test_build_evidence_from_metadata(tmp_path: Path):
    (tmp_path / "media_metadata.jsonl").write_text(
        '{"source_video_id":"video_1","filename":"a.mp4","date_candidates":[{"field":"CreateDate","raw":"2026:01:01 00:00:00","confidence":0.65}]}\n',
        encoding="utf-8",
    )
    result = build_evidence(tmp_path)

    assert result["evidence_count"] == 1
    assert result["by_kind"]["date_candidate"] == 1

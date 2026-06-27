from pathlib import Path

from tapesplit.evidence import build_evidence, evidence_for_prompt


def test_build_evidence_from_metadata(tmp_path: Path):
    (tmp_path / "media_metadata.jsonl").write_text(
        '{"source_video_id":"video_1","filename":"a.mp4","date_candidates":[{"field":"CreateDate","raw":"2026:01:01 00:00:00","confidence":0.65}]}\n',
        encoding="utf-8",
    )
    result = build_evidence(tmp_path)

    assert result["evidence_count"] == 1
    assert result["by_kind"]["date_candidate"] == 1


def test_build_evidence_from_visual_text_observations(tmp_path: Path):
    (tmp_path / "visual_text_observations.jsonl").write_text(
        (
            '{"id":"visual_text_000001","visual_asset_id":"visual_asset_000001",'
            '"source_video_id":"video_1","source_subject_type":"event",'
            '"source_subject_id":"canonical_event_000001","time_s":42,'
            '"text":"Roosevelt Middle School","confidence":0.88,'
            '"engine":"apple_vision_recognize_text","text_backend":"apple-vision"}\n'
        ),
        encoding="utf-8",
    )

    result = build_evidence(tmp_path)

    assert result["evidence_count"] == 1
    assert result["by_kind"]["ocr_text"] == 1
    assert result["by_modality"]["visual_text"] == 1


def test_evidence_for_prompt_prioritizes_content_over_metadata(tmp_path: Path):
    rows = []
    for index in range(130):
        rows.append(
            (
                '{"id":"ev_meta_%03d","source_video_id":"video_1","kind":"date_candidate",'
                '"modality":"metadata","text":"CreateDate: 2026:01:01","confidence":0.3}'
            )
            % index
        )
    rows.append(
        (
            '{"id":"ev_content","source_video_id":"video_2","kind":"twelvelabs_search_transcript",'
            '"modality":"transcript","start_s":12,"end_s":18,"text":"Philip blows out birthday candles",'
            '"confidence":0.7}'
        )
    )
    (tmp_path / "evidence.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    prompt_evidence = evidence_for_prompt(tmp_path, limit=5)

    assert prompt_evidence[0]["id"] == "ev_content"
    assert prompt_evidence[0]["source_video_id"] == "video_2"

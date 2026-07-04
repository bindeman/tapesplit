import json
from pathlib import Path

from tapesplit.report import export_review_report
from tapesplit.search import build_search_index, query_search_index


def test_search_omits_records_inside_excluded_event_ranges(tmp_path: Path):
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 12,
                "text": "Alice meets the Queen of Hearts in Wonderland.",
            },
            {
                "id": "tr_000002",
                "source_video_id": "video_000001",
                "start_s": 130,
                "end_s": 136,
                "text": "Filip blows out birthday candles.",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Alice in Wonderland Broadcast",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 100,
                "relatedness": "likely_unrelated",
                "metadata": {"people": ["Alice"], "place_candidates": ["Wonderland"]},
            },
            {
                "id": "canonical_event_000002",
                "title": "Family Birthday",
                "source_video_id": "video_000001",
                "start_s": 120,
                "end_s": 200,
                "relatedness": "likely_family",
                "summary": "Filip celebrates a birthday.",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {
                "id": "gem_ev_000001",
                "source_video_id": "video_000001",
                "start_s": 20,
                "end_s": 24,
                "kind": "gemini_person_mention",
                "text": "Alice",
            },
            {
                "id": "gem_ev_000002",
                "source_video_id": "video_000001",
                "start_s": 140,
                "end_s": 144,
                "kind": "gemini_event_candidate",
                "text": "birthday candles",
            },
        ],
    )

    result = build_search_index(tmp_path)

    assert result["by_type"] == {"transcript": 1, "evidence": 1, "event": 1}
    assert query_search_index(tmp_path, "Wonderland Alice", limit=5)["results"] == []
    birthday = query_search_index(tmp_path, "birthday candles", limit=5)
    result_ids = {(row["record_type"], row["source_id"]) for row in birthday["results"]}
    assert ("transcript", "tr_000002") in result_ids


def test_search_omits_records_inside_gemini_unrelated_ranges(tmp_path: Path):
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 610,
                "end_s": 612,
                "text": "Alice, you promised me and your father.",
            },
            {
                "id": "tr_000002",
                "source_video_id": "video_000001",
                "start_s": 900,
                "end_s": 905,
                "text": "Filip opens birthday gifts.",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_analyses.jsonl",
        [
            {
                "analysis_run_id": "gem_run",
                "source_video_id": "video_000001",
                "chunk_start_s": 600,
                "time_basis": "chunk",
                "analysis": {
                    "unrelated_ranges": [
                        {"start_s": 0, "end_s": 100, "reason": "movie_or_tv"}
                    ]
                },
            }
        ],
    )

    result = build_search_index(tmp_path)

    assert result["by_type"] == {"transcript": 1}
    assert query_search_index(tmp_path, "Alice father", limit=5)["results"] == []
    birthday = query_search_index(tmp_path, "birthday gifts", limit=5)
    assert birthday["results"][0]["source_id"] == "tr_000002"


def test_search_omits_entire_source_when_only_events_are_excluded(tmp_path: Path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 1000}}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 100,
                "relatedness": "likely_unrelated",
                "title": "TV Broadcast",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 600,
                "end_s": 604,
                "text": "Alice, you promised me and your father.",
            }
        ],
    )

    result = build_search_index(tmp_path)

    assert result["documents"] == 0
    assert query_search_index(tmp_path, "Alice father", limit=5)["results"] == []


def test_report_counts_visible_claims_and_evidence_only(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 100,
                "relatedness": "likely_unrelated",
                "title": "TV Broadcast",
            },
            {
                "id": "canonical_event_000002",
                "source_video_id": "video_000001",
                "start_s": 120,
                "end_s": 180,
                "relatedness": "likely_family",
                "title": "Family Clip",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {"id": "gem_ev_000001", "source_video_id": "video_000001", "start_s": 10, "end_s": 20, "text": "Alice"},
            {"id": "gem_ev_000002", "source_video_id": "video_000001", "start_s": 130, "end_s": 140, "text": "Filip"},
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_claims.jsonl",
        [
            {"id": "gem_claim_000001", "predicate": "person_mention", "value": "Alice", "evidence_ids": ["gem_ev_000001"]},
            {"id": "gem_claim_000002", "predicate": "person_mention", "value": "Filip", "evidence_ids": ["gem_ev_000002"]},
        ],
    )
    _write_jsonl(
        tmp_path / "albums.jsonl",
        [
            {"id": "album_000001", "title": "TV Broadcast", "album_type": "unrelated_content", "export_status": "excluded"},
            {"id": "album_000002", "title": "Family Clip", "album_type": "day", "export_status": "candidate"},
        ],
    )

    result = export_review_report(tmp_path)
    html = (tmp_path / "review.html").read_text(encoding="utf-8")

    assert result["evidence"] == 1
    assert result["claims"] == 1
    assert result["events"] == 1
    assert result["albums"] == 1
    assert "Alice" not in html
    assert "Filip" in html


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_family_events_outrank_coarse_exclusion_ranges():
    from tapesplit.visibility import ExclusionRange, VisibilityFilter

    coarse = VisibilityFilter(
        [ExclusionRange(source_video_id="video_000001", start_s=0.0, end_s=5000.0, reason="unrelated", source_id="x")]
    )
    family_event = {
        "id": "e1",
        "relatedness": "likely_family",
        "metadata": {"source_ranges": [{"source_video_id": "video_000001", "start_s": 1235.0, "end_s": 1840.0}]},
    }
    unrelated_event = {
        "id": "e2",
        "relatedness": "likely_unrelated",
        "metadata": {"source_ranges": [{"source_video_id": "video_000001", "start_s": 1235.0, "end_s": 1840.0}]},
    }
    assert coarse.excluded_row(family_event) is False
    assert coarse.excluded_row(unrelated_event) is True

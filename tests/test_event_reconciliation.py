import json

from tapesplit.event_reconciliation import build_event_reconciliations
from tapesplit.storage import read_jsonl


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_build_event_reconciliations_retitles_drifted_place_claim_from_local_context(tmp_path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 700}}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Alaska Trip and Hot Tub Relaxation",
                "summary": "People are possibly on a tour in Alaska.",
                "metadata": {
                    "event_type": "travel",
                    "people": ["Russians", "host"],
                    "place_candidates": ["Alaska", "Anchorage"],
                    "date_candidates": ["JUN 12 2002"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 500, "end_s": 560}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "event_alignments.jsonl",
        [
            {
                "id": "event_alignment_000001",
                "canonical_event_id": "canonical_event_000001",
                "event_title": "Alaska Trip and Hot Tub Relaxation",
                "timing_status": "possible_misaligned",
                "support_score": 0.269,
                "source_video_ids": ["video_000001"],
                "source_ranges": [{"source_video_id": "video_000001", "start_s": 500, "end_s": 560}],
                "suggested_source_ranges": [
                    {
                        "source_video_id": "video_000001",
                        "start_s": 235,
                        "end_s": 307,
                        "transcript_ids": ["tr_000954"],
                    }
                ],
                "transcript_context_anchors": [
                    {
                        "label": "farmhouse / farm area",
                        "role": "generic_place_context",
                        "confidence": 0.66,
                        "distance_to_event_s": 33.64,
                        "transcript_id": "tr_001014",
                        "text": "Фермерский домик с русскими картинами.",
                    },
                    {
                        "label": "Whitewater",
                        "role": "spoken_location_anchor",
                        "confidence": 0.82,
                        "distance_to_event_s": 145.64,
                        "transcript_id": "tr_000972",
                        "text": "Город называется Whitewater.",
                    },
                ],
                "evidence_claims": [{"status": "relocated_transcript"}],
                "entity_support": {
                    "places": [
                        {
                            "value": "Alaska",
                            "status": "model_evidence",
                            "place_role": {
                                "role": "ambiguous_place_reference",
                                "include_in_place_groups": False,
                            },
                        },
                        {
                            "value": "Anchorage",
                            "status": "ambiguous_place_reference",
                            "place_role": {
                                "role": "ambiguous_place_reference",
                                "include_in_place_groups": False,
                            },
                        },
                    ]
                },
            }
        ],
    )

    result = build_event_reconciliations(tmp_path)
    rows = read_jsonl(tmp_path / "event_reconciliations.jsonl")

    assert result["by_status"] == {"corrected": 1}
    assert rows[0]["reconciled_title"] == "Whitewater Farmhouse Visit"
    assert rows[0]["reconciliation_status"] == "corrected"
    assert rows[0]["review_status"] == "unreviewed"
    assert rows[0]["selected_place_labels"] == ["Whitewater", "farmhouse / farm area"]
    assert rows[0]["rejected_place_labels"] == ["Alaska", "Anchorage"]
    assert rows[0]["selected_source_ranges"][0]["start_s"] == 500
    assert rows[0]["relocated_evidence_ranges"][0]["transcript_ids"] == ["tr_000954"]


def test_build_event_reconciliations_keeps_supported_title(tmp_path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 300}}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday Party",
                "summary": "Filip blows out candles.",
                "metadata": {
                    "event_type": "birthday",
                    "people": ["Filip"],
                    "place_candidates": ["home"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 100, "end_s": 150}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "event_alignments.jsonl",
        [
            {
                "id": "event_alignment_000001",
                "canonical_event_id": "canonical_event_000001",
                "event_title": "Birthday Party",
                "timing_status": "aligned",
                "support_score": 0.9,
                "source_video_ids": ["video_000001"],
                "source_ranges": [{"source_video_id": "video_000001", "start_s": 100, "end_s": 150}],
                "entity_support": {"places": [{"value": "home", "status": "model_evidence"}]},
            }
        ],
    )

    build_event_reconciliations(tmp_path)
    rows = read_jsonl(tmp_path / "event_reconciliations.jsonl")

    assert rows[0]["reconciled_title"] == "Birthday Party"
    assert rows[0]["reconciliation_status"] == "accepted"
    assert rows[0]["title_status"] == "original_supported"

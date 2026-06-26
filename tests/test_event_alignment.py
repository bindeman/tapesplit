import json

from tapesplit.event_alignment import build_event_alignments
from tapesplit.storage import read_jsonl


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_build_event_alignments_scores_nearby_transcript_support(tmp_path):
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
                "summary": "Filip blows out candles on a cake.",
                "evidence_ids": ["gem_ev_000001"],
                "metadata": {
                    "people": ["Filip"],
                    "place_candidates": ["home"],
                    "date_candidates": ["MAR 3 2001"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 100, "end_s": 150}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {
                "id": "gem_ev_000001",
                "source_video_id": "video_000001",
                "start_s": 100,
                "end_s": 150,
                "kind": "gemini_event_candidate",
                "text": "Filip birthday at home on MAR 3 2001.",
                "confidence": 0.9,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 112,
                "end_s": 118,
                "text": "Happy birthday Filip, blow out the candles on the cake.",
            }
        ],
    )

    result = build_event_alignments(tmp_path, context_seconds=10)
    rows = read_jsonl(tmp_path / "event_alignments.jsonl")

    assert result["event_alignments"] == 1
    assert rows[0]["timing_status"] == "aligned"
    assert rows[0]["support_score"] > 0.5
    assert rows[0]["transcript_support"][0]["transcript_id"] == "tr_000001"
    assert rows[0]["entity_support"]["people"][0]["status"] == "direct_transcript"


def test_build_event_alignments_flags_stronger_outside_anchor(tmp_path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 700}}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday Party",
                "summary": "Family birthday with cake.",
                "metadata": {
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 100, "end_s": 120}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 500,
                "end_s": 506,
                "text": "Birthday party and cake for the whole family.",
            }
        ],
    )

    build_event_alignments(tmp_path, context_seconds=10)
    rows = read_jsonl(tmp_path / "event_alignments.jsonl")

    assert rows[0]["timing_status"] == "possible_misaligned"
    assert rows[0]["alternate_transcript_anchors"][0]["transcript_id"] == "tr_000001"
    assert "stronger transcript match appears outside event range" in rows[0]["warnings"]


def test_build_event_alignments_matches_project_aliases_across_scripts(tmp_path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 300}}],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip / Filya",
                "aliases": ["Filip", "Filya"],
                "metadata": {"normalized_key": "filipp"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Filip Birthday",
                "summary": "Filip blows out two candles.",
                "metadata": {
                    "people": ["Filip"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 100, "end_s": 150}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 112,
                "end_s": 118,
                "text": "Вот Филиппу купили пирог. Зажгли две свечки.",
            }
        ],
    )

    build_event_alignments(tmp_path, context_seconds=10)
    rows = read_jsonl(tmp_path / "event_alignments.jsonl")

    assert rows[0]["timing_status"] == "aligned"
    assert rows[0]["entity_support"]["people"][0]["status"] == "direct_transcript"
    assert rows[0]["transcript_support"][0]["matched_entities"] == ["Filip"]


def test_build_event_alignments_relocates_quote_evidence_and_marks_model_only_reviewable(tmp_path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 700}}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Anchorage Visit",
                "summary": "People spend time in Anchorage.",
                "evidence_ids": ["gem_ev_000001"],
                "metadata": {
                    "place_candidates": ["Anchorage"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 500, "end_s": 560}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {
                "id": "gem_ev_000001",
                "source_video_id": "video_000001",
                "start_s": 500,
                "end_s": 560,
                "kind": "gemini_event_candidate",
                "text": "People are in Anchorage.",
                "metadata": {"evidence_text": ["Вот так проводят время господа русские в Анкоридже."]},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 200,
                "end_s": 205,
                "text": "Вот так проводят время господа русские в Анкоридже.",
            },
            {
                "id": "tr_000002",
                "source_video_id": "video_000001",
                "start_s": 520,
                "end_s": 525,
                "text": "Фермерский домик с русскими картинами.",
            },
        ],
    )

    build_event_alignments(tmp_path, context_seconds=20)
    rows = read_jsonl(tmp_path / "event_alignments.jsonl")

    assert rows[0]["timing_status"] == "possible_misaligned"
    assert rows[0]["suggested_review_status"] == "needs_review"
    assert rows[0]["evidence_claims"][0]["status"] == "relocated_transcript"
    assert rows[0]["suggested_source_ranges"][0]["transcript_ids"] == ["tr_000001"]


def test_build_event_alignments_extracts_nearby_transcript_location_anchors(tmp_path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "probe": {"duration_s": 700}}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Farmhouse Visit",
                "summary": "Family visits a farm house.",
                "metadata": {
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 500, "end_s": 560}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 430,
                "end_s": 432,
                "text": "Город называется Whitewater.",
            },
            {
                "id": "tr_000002",
                "source_video_id": "video_000001",
                "start_s": 520,
                "end_s": 525,
                "text": "Фермерский домик с русскими картинами.",
            },
        ],
    )

    build_event_alignments(tmp_path, context_seconds=20)
    rows = read_jsonl(tmp_path / "event_alignments.jsonl")

    anchors = rows[0]["transcript_context_anchors"]
    assert any(anchor["label"] == "Whitewater" and anchor["role"] == "spoken_location_anchor" for anchor in anchors)
    assert any(anchor["label"] == "farmhouse / farm area" for anchor in anchors)

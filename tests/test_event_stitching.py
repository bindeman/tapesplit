from pathlib import Path

from tapesplit.event_stitching import stitch_events, stitch_project_events
from tapesplit.storage import read_jsonl


def test_stitch_events_merges_boundary_split_same_event():
    events = [
        {
            "id": "gem_event_000007",
            "title": "Hiking Spencer Butte",
            "start_s": 1687,
            "end_s": 1785,
            "confidence": 0.95,
            "evidence_ids": ["ev_1"],
            "summary": "Filip hikes Spencer Butte.",
            "relatedness": "likely_family",
            "review_status": "needs_review",
            "metadata": {
                "event_type": "travel",
                "place_candidates": ["Spencer Butte", "Oregon"],
                "people": ["Filip"],
                "date_candidates": ["FEB 19 2006"],
                "validation_notes": ["end_clamped_to_chunk_duration"],
            },
        },
        {
            "id": "gem_event_000008",
            "title": "Hiking Spencer Butte, Oregon",
            "start_s": 1770,
            "end_s": 2670,
            "confidence": 0.9,
            "evidence_ids": ["ev_2"],
            "summary": "The family continues the Spencer Butte hike.",
            "relatedness": "likely_family",
            "review_status": "needs_review",
            "metadata": {
                "event_type": "travel",
                "place_candidates": ["Eugene, Oregon", "Spencer Butte"],
                "people": ["Philip"],
                "validation_notes": ["end_clamped_to_chunk_duration"],
            },
        },
    ]

    stitched = stitch_events(events)

    assert len(stitched) == 1
    assert stitched[0]["start_s"] == 1687
    assert stitched[0]["end_s"] == 2670
    assert stitched[0]["evidence_ids"] == ["ev_1", "ev_2"]
    assert stitched[0]["metadata"]["source_event_ids"] == ["gem_event_000007", "gem_event_000008"]
    assert stitched[0]["metadata"]["source_event_count"] == 2


def test_stitch_events_does_not_merge_chunk_overlap_with_different_topic():
    events = [
        {
            "id": "gem_event_000004",
            "title": "Filip's First Day of School in the US",
            "start_s": 744,
            "end_s": 900,
            "confidence": 0.9,
            "metadata": {
                "event_type": "school",
                "place_candidates": ["Maplewood School"],
                "people": ["Filip"],
                "date_candidates": ["SEP 7 2005"],
                "validation_notes": ["end_clamped_to_chunk_duration"],
            },
        },
        {
            "id": "gem_event_000005",
            "title": "Tooth Extraction",
            "start_s": 885,
            "end_s": 1137,
            "confidence": 0.95,
            "metadata": {
                "event_type": "medical",
                "place_candidates": ["Living Room"],
                "people": ["Filip"],
            },
        },
    ]

    stitched = stitch_events(events)

    assert [event["title"] for event in stitched] == [
        "Filip's First Day of School in the US",
        "Tooth Extraction",
    ]


def test_stitch_events_does_not_merge_on_generic_place_only():
    events = [
        {
            "id": "gem_event_000010",
            "title": "Exploring Volcanic Landscapes",
            "start_s": 2860,
            "end_s": 3259,
            "confidence": 0.9,
            "metadata": {
                "event_type": "travel",
                "place_candidates": ["Hualalai", "Hawaii"],
            },
        },
        {
            "id": "gem_event_000011",
            "title": "Lava Flowing into the Ocean",
            "start_s": 3259,
            "end_s": 3555,
            "confidence": 0.9,
            "metadata": {
                "event_type": "travel",
                "place_candidates": ["Kilauea Volcano", "Hawaii"],
                "validation_notes": ["end_clamped_to_chunk_duration"],
            },
        },
    ]

    stitched = stitch_events(events)

    assert [event["title"] for event in stitched] == [
        "Exploring Volcanic Landscapes",
        "Lava Flowing into the Ocean",
    ]


def test_stitch_project_events_prefers_gemini_events(tmp_path: Path):
    (tmp_path / "events.jsonl").write_text(
        '{"id":"event_000001","title":"Legacy broad event","start_s":0,"end_s":1000}\n',
        encoding="utf-8",
    )
    (tmp_path / "gemini_events.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"id":"gem_event_000001","title":"Hiking Spencer Butte",'
                    '"start_s":10,"end_s":100,"confidence":0.9,'
                    '"evidence_ids":["ev_1"],"metadata":{"event_type":"travel",'
                    '"place_candidates":["Spencer Butte"]}}'
                ),
                (
                    '{"id":"gem_event_000002","title":"Hiking Spencer Butte, Oregon",'
                    '"start_s":95,"end_s":200,"confidence":0.9,'
                    '"evidence_ids":["ev_2"],"metadata":{"event_type":"travel",'
                    '"place_candidates":["Spencer Butte"]}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = stitch_project_events(tmp_path)
    canonical = read_jsonl(tmp_path / "canonical_events.jsonl")

    assert result["source_events"] == 2
    assert result["canonical_events"] == 1
    assert canonical[0]["metadata"]["source_event_ids"] == ["gem_event_000001", "gem_event_000002"]


def test_stitch_project_events_can_merge_event_across_tapes(tmp_path: Path):
    (tmp_path / "tapes.jsonl").write_text(
        "\n".join(
            [
                '{"id":"video_000001","filename":"tape-1.mp4","probe":{"duration_s":100}}',
                '{"id":"video_000002","filename":"tape-2.mp4","probe":{"duration_s":80}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gemini_evidence.jsonl").write_text(
        "\n".join(
            [
                '{"id":"ev_1","source_video_id":"video_000001","start_s":82,"end_s":100}',
                '{"id":"ev_2","source_video_id":"video_000002","start_s":0,"end_s":18}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gemini_events.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"id":"gem_event_000001","title":"Birthday Party",'
                    '"start_s":82,"end_s":100,"confidence":0.9,'
                    '"evidence_ids":["ev_1"],"metadata":{"event_type":"birthday",'
                    '"place_candidates":["Living Room"],"people":["Philip"],'
                    '"date_candidates":["APR 3 2006"],'
                    '"validation_notes":["end_clamped_to_tape_end"]}}'
                ),
                (
                    '{"id":"gem_event_000002","title":"Birthday Party Continues",'
                    '"start_s":0,"end_s":18,"confidence":0.88,'
                    '"evidence_ids":["ev_2"],"metadata":{"event_type":"birthday",'
                    '"place_candidates":["Living Room"],"people":["Philip"],'
                    '"date_candidates":["APR 3 2006"]}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = stitch_project_events(tmp_path, max_gap_seconds=30)
    canonical = read_jsonl(tmp_path / "canonical_events.jsonl")

    assert result["canonical_events"] == 1
    assert canonical[0]["start_s"] == 82
    assert canonical[0]["end_s"] == 118
    assert canonical[0]["metadata"]["source_video_ids"] == ["video_000001", "video_000002"]
    assert canonical[0]["metadata"]["source_ranges"] == [
        {
            "source_video_id": "video_000001",
            "start_s": 82.0,
            "end_s": 100.0,
            "timeline_start_s": 82.0,
            "timeline_end_s": 100.0,
        },
        {
            "source_video_id": "video_000002",
            "start_s": 0.0,
            "end_s": 18.0,
            "timeline_start_s": 100.0,
            "timeline_end_s": 118.0,
        },
    ]
    assert "cross_tape_candidate" in canonical[0]["metadata"]["merge_reasons"][0]

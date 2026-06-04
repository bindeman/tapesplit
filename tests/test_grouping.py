import json
from pathlib import Path

from tapesplit.grouping import build_project_groups, _parse_date_candidate
from tapesplit.storage import read_jsonl


def test_parse_date_candidate_distinguishes_event_dates_from_historical_mentions():
    assert _parse_date_candidate("MAR 22 2006") == {
        "date_value": "2006-03-22",
        "precision": "day",
        "source_kind": "event_date_candidate",
        "excluded_as_event_date": False,
    }
    assert _parse_date_candidate("1974 (eruption)") == {
        "date_value": "1974",
        "precision": "year",
        "source_kind": "mentioned_historical_date",
        "excluded_as_event_date": True,
    }
    assert _parse_date_candidate("mid-90s") == {
        "date_value": "1990s",
        "precision": "decade",
        "source_kind": "mentioned_historical_date",
        "excluded_as_event_date": True,
    }


def test_build_project_groups_writes_reviewable_indexes(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "First Day of School in Moscow",
                "start_s": 44,
                "end_s": 720,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "metadata": {
                    "event_type": "school",
                    "people": ["Filip", "Lyudmila Petrovna"],
                    "place_candidates": ["Moscow", "School No. 123"],
                    "date_candidates": ["SEP 1 2005"],
                    "languages": ["Russian"],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "Home in Moscow",
                "start_s": 720,
                "end_s": 743,
                "confidence": 0.8,
                "evidence_ids": ["ev_2"],
                "metadata": {
                    "event_type": "home",
                    "place_candidates": ["Moscow", "Kitchen"],
                    "date_candidates": ["SEP 1 2005"],
                    "languages": ["Russian"],
                },
            },
            {
                "id": "canonical_event_000003",
                "title": "Filip's First Day of School in the US",
                "start_s": 744,
                "end_s": 900,
                "confidence": 0.9,
                "evidence_ids": ["ev_3"],
                "metadata": {
                    "event_type": "school",
                    "people": ["Filip", "Emily"],
                    "place_candidates": ["Maplewood School"],
                    "date_candidates": ["SEP 7 2005"],
                    "languages": ["Russian", "English"],
                },
            },
            {
                "id": "canonical_event_000004",
                "title": "Tooth Extraction",
                "start_s": 885,
                "end_s": 1137,
                "confidence": 0.95,
                "evidence_ids": ["ev_4"],
                "metadata": {
                    "event_type": "medical",
                    "people": ["Philip"],
                    "place_candidates": ["Living Room"],
                    "languages": ["Russian"],
                },
            },
            {
                "id": "canonical_event_000005",
                "title": "Exploring Volcanic Landscapes",
                "start_s": 2860,
                "end_s": 3259,
                "confidence": 0.9,
                "evidence_ids": ["ev_5"],
                "metadata": {
                    "event_type": "travel",
                    "people": ["Filip"],
                    "place_candidates": ["Hualalai", "Hawaii"],
                    "date_candidates": ["MAR 22 2006", "1856"],
                    "languages": ["Russian"],
                },
            },
            {
                "id": "canonical_event_000006",
                "title": "Lava Flowing into the Ocean",
                "start_s": 3259,
                "end_s": 3555,
                "confidence": 0.9,
                "evidence_ids": ["ev_6"],
                "metadata": {
                    "event_type": "travel",
                    "people": ["Filip"],
                    "place_candidates": ["Kilauea Volcano", "Hawaii"],
                    "languages": ["Russian", "English"],
                },
            },
            {
                "id": "canonical_event_000007",
                "title": "Botanical Garden Visit in Hilo",
                "start_s": 3980,
                "end_s": 4150,
                "confidence": 0.9,
                "evidence_ids": ["ev_7"],
                "metadata": {
                    "event_type": "travel",
                    "people": ["Filip"],
                    "place_candidates": ["Hilo", "botanical garden"],
                    "date_candidates": ["MAR 26 2006"],
                    "languages": ["Russian"],
                },
            },
            {
                "id": "canonical_event_000008",
                "title": "Child Greeting Grandparents",
                "start_s": 4200,
                "end_s": 4250,
                "confidence": 0.8,
                "evidence_ids": ["ev_8"],
                "metadata": {
                    "event_type": "family",
                    "people": ["Филя"],
                    "languages": ["Russian"],
                },
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [{"id": f"ev_{index}", "source_video_id": "video_000001"} for index in range(1, 9)],
    )

    result = build_project_groups(tmp_path)

    assert result["source_events"] == 8
    assert result["people_groups"] == 3
    assert result["albums"] >= 4

    people = read_jsonl(tmp_path / "people_groups.jsonl")
    filip = next(group for group in people if group["metadata"]["normalized_key"] == "filip")
    assert filip["aliases"] == ["Filip", "Philip", "Филя"]
    assert filip["review_status"] == "needs_review"
    assert filip["canonical_event_ids"] == [
        "canonical_event_000001",
        "canonical_event_000003",
        "canonical_event_000004",
        "canonical_event_000005",
        "canonical_event_000006",
        "canonical_event_000007",
        "canonical_event_000008",
    ]

    dates = read_jsonl(tmp_path / "date_groups.jsonl")
    historical = next(group for group in dates if group["date_value"] == "1856")
    assert historical["excluded_as_event_date"] is True
    assert historical["source_kind"] == "mentioned_historical_date"

    event_groups = read_jsonl(tmp_path / "event_groups.jsonl")
    hawaii_group = next(group for group in event_groups if group["title"] == "Hawaii Trip")
    assert hawaii_group["canonical_event_ids"] == [
        "canonical_event_000005",
        "canonical_event_000006",
        "canonical_event_000007",
    ]
    assert hawaii_group["review_status"] == "needs_review"

    albums = read_jsonl(tmp_path / "albums.jsonl")
    sep7_album = next(album for album in albums if "Sep 7, 2005" in album["title"])
    assert sep7_album["canonical_event_ids"] == ["canonical_event_000003", "canonical_event_000004"]
    assert sep7_album["review_status"] == "needs_review"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

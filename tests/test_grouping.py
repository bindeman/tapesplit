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
                    "people": ["Filya", "Филя"],
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
    filip = next(group for group in people if group["metadata"]["normalized_key"] == "filipp")
    assert filip["aliases"] == ["Filip", "Filya", "Philip", "Филя"]
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


def test_build_project_groups_excludes_unrelated_people_from_context_groups(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Alice in Wonderland Broadcast",
                "start_s": 0,
                "end_s": 100,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "relatedness": "likely_unrelated",
                "metadata": {
                    "event_type": "tv",
                    "people": ["Alice"],
                    "place_candidates": ["Wonderland"],
                    "languages": ["English"],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "Family Birthday",
                "start_s": 120,
                "end_s": 200,
                "confidence": 0.9,
                "evidence_ids": ["ev_2"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "family",
                    "people": ["Filip"],
                    "place_candidates": ["Home"],
                    "languages": ["Russian"],
                },
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {"id": "ev_1", "source_video_id": "video_000001"},
            {"id": "ev_2", "source_video_id": "video_000001"},
        ],
    )

    result = build_project_groups(tmp_path)
    people = read_jsonl(tmp_path / "people_groups.jsonl")
    albums = read_jsonl(tmp_path / "albums.jsonl")

    assert result["source_events"] == 2
    assert result["context_events"] == 1
    assert [group["label"] for group in people] == ["Filip"]
    unrelated_album = next(album for album in albums if album["title"] == "Alice in Wonderland Broadcast")
    assert unrelated_album["album_type"] == "unrelated_content"
    assert unrelated_album["export_status"] == "excluded"


def test_build_project_groups_merges_cross_script_and_nickname_person_variants(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Class Party",
                "start_s": 0,
                "end_s": 100,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "metadata": {
                    "event_type": "party",
                    "people": ["Semyon", "Syoma", "Сема", "СЁМА", "Tania", "Tanya", "Sveta", "Света"],
                },
            }
        ],
    )
    _write_jsonl(tmp_path / "gemini_evidence.jsonl", [{"id": "ev_1", "source_video_id": "video_000001"}])

    result = build_project_groups(tmp_path)
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert result["people_groups"] == 3
    assert next(group for group in people if group["metadata"]["normalized_key"] == "semyon")["aliases"] == [
        "Semyon",
        "Syoma",
        "Сема",
        "СЁМА",
    ]
    assert next(group for group in people if group["metadata"]["normalized_key"] == "tatiana")["aliases"] == ["Tania", "Tanya"]
    assert next(group for group in people if group["metadata"]["normalized_key"] == "svetlana")["aliases"] == ["Sveta", "Света"]


def test_build_project_groups_scopes_role_people_to_event_context(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday Party",
                "start_s": 0,
                "end_s": 100,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "metadata": {"event_type": "birthday", "people": ["host"]},
            },
            {
                "id": "canonical_event_000002",
                "title": "New Year Party",
                "start_s": 120,
                "end_s": 220,
                "confidence": 0.9,
                "evidence_ids": ["ev_2"],
                "metadata": {"event_type": "holiday", "people": ["host"]},
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {"id": "ev_1", "source_video_id": "video_000001"},
            {"id": "ev_2", "source_video_id": "video_000001"},
        ],
    )

    result = build_project_groups(tmp_path)
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert result["people_groups"] == 2
    assert {group["label"] for group in people} == {"host (Birthday Party)", "host (New Year Party)"}
    assert all(group["kind"] == "role_candidate" for group in people)


def test_build_project_groups_merges_place_admin_aliases_and_scopes_generic_places(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Madison Holiday",
                "start_s": 0,
                "end_s": 100,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "holiday",
                    "place_candidates": ["Madison, USA"],
                    "date_candidates": ["JAN 5 2003"],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "Nursery School Activities",
                "start_s": 110,
                "end_s": 220,
                "confidence": 0.9,
                "evidence_ids": ["ev_2"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "school",
                    "place_candidates": ["Lakeside Village Nursery School"],
                },
            },
            {
                "id": "canonical_event_000003",
                "title": "Madison Matinee",
                "start_s": 225,
                "end_s": 330,
                "confidence": 0.9,
                "evidence_ids": ["ev_3"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "school",
                    "place_candidates": ["Madison, Wisconsin, USA"],
                },
            },
            {
                "id": "canonical_event_000004",
                "title": "School Hall Cleanup",
                "start_s": 340,
                "end_s": 430,
                "confidence": 0.9,
                "evidence_ids": ["ev_4"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "school",
                    "place_candidates": ["Russian School", "Madison", "school hall"],
                },
            },
            {
                "id": "canonical_event_000005",
                "title": "Madison Home Morning",
                "start_s": 500,
                "end_s": 600,
                "confidence": 0.9,
                "evidence_ids": ["ev_5"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "home",
                    "place_candidates": ["home", "Madison"],
                },
            },
            {
                "id": "canonical_event_000006",
                "title": "Oregon Home Afternoon",
                "start_s": 10000,
                "end_s": 10100,
                "confidence": 0.9,
                "evidence_ids": ["ev_6"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "home",
                    "place_candidates": ["home", "Eugene, Oregon, USA"],
                },
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [{"id": f"ev_{index}", "source_video_id": "video_000001"} for index in range(1, 7)],
    )

    build_project_groups(tmp_path)
    places = read_jsonl(tmp_path / "place_groups.jsonl")

    madison = next(group for group in places if group["label"] == "Madison, Wisconsin, USA")
    assert madison["aliases"] == ["Madison", "Madison, USA", "Madison, Wisconsin, USA"]
    assert madison["canonical_event_ids"] == [
        "canonical_event_000001",
        "canonical_event_000003",
        "canonical_event_000004",
        "canonical_event_000005",
    ]

    nursery = next(group for group in places if group["label"] == "Lakeside Village Nursery School")
    assert "Madison, Wisconsin, USA" in nursery["parent_place_labels"]

    school_hall = next(group for group in places if group["label"] == "school hall")
    assert "Russian School" in school_hall["parent_place_labels"]
    assert "Madison, Wisconsin, USA" in school_hall["parent_place_labels"]
    assert school_hall["review_status"] == "needs_review"

    home_groups = [group for group in places if group["label"] == "home"]
    assert len(home_groups) == 2
    assert sorted(group["scope_label"] for group in home_groups) == [
        "Eugene, Oregon, USA context",
        "Madison, Wisconsin, USA context",
    ]


def test_build_project_groups_uses_spelling_normalization_and_project_person_aliases(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Backyard Cookout",
                "start_s": 0,
                "end_s": 100,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "relatedness": "likely_family",
                "metadata": {"people": ["Philip", "Phillip", "Mimi"]},
            },
            {
                "id": "canonical_event_000002",
                "title": "Kitchen Visit",
                "start_s": 120,
                "end_s": 200,
                "confidence": 0.9,
                "evidence_ids": ["ev_2"],
                "relatedness": "likely_family",
                "metadata": {"people": ["Miriam"]},
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {"id": "ev_1", "source_video_id": "video_000001"},
            {"id": "ev_2", "source_video_id": "video_000001"},
        ],
    )
    (tmp_path / "person_aliases.json").write_text(
        json.dumps({"aliases": [{"canonical": "Miriam", "aliases": ["Mimi"]}]}),
        encoding="utf-8",
    )

    build_project_groups(tmp_path)
    people = read_jsonl(tmp_path / "people_groups.jsonl")

    assert sorted(group["aliases"] for group in people) == [["Mimi", "Miriam"], ["Philip", "Phillip"]]
    assert all(group["review_status"] == "needs_review" for group in people)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

import json
from pathlib import Path

from tapesplit.grouping import build_project_groups
from tapesplit.place_roles import build_place_roles_for_project
from tapesplit.storage import read_jsonl


def test_place_roles_keep_travel_plans_out_of_place_groups_and_albums(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Madison Spring Picnic",
                "start_s": 10,
                "end_s": 120,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "home",
                    "people": ["Filip"],
                    "place_candidates": ["Madison, Wisconsin, USA", "park"],
                    "date_candidates": ["MAY 10 2002"],
                    "source_video_ids": ["video_000001"],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "May Activities and Travel Plans",
                "start_s": 130,
                "end_s": 240,
                "confidence": 0.9,
                "evidence_ids": ["ev_2"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "travel",
                    "people": ["Filip"],
                    "place_candidates": ["park", "Alaska"],
                    "date_candidates": ["MAY 10 2002"],
                    "source_video_ids": ["video_000001"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 130, "end_s": 240}],
                },
                "summary": "The family plays in a park and buys tickets to Russia and Alaska.",
            },
            {
                "id": "canonical_event_000003",
                "title": "Rain Gear and Hot Tub",
                "start_s": 250,
                "end_s": 320,
                "confidence": 0.8,
                "evidence_ids": ["ev_3"],
                "relatedness": "likely_family",
                "metadata": {
                    "event_type": "travel",
                    "people": ["host"],
                    "place_candidates": ["Alaska", "Anchorage"],
                    "date_candidates": ["MAY 10 2002"],
                    "source_video_ids": ["video_000001"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 250, "end_s": 320}],
                },
                "summary": "People are seen in rain gear by a bus, possibly on a tour in Alaska.",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "gemini_evidence.jsonl",
        [
            {"id": "ev_1", "kind": "gemini_event_candidate", "source_video_id": "video_000001", "text": "Picnic in Madison."},
            {
                "id": "ev_2",
                "kind": "gemini_event_candidate",
                "source_video_id": "video_000001",
                "text": "The family buys tickets to Russia and Alaska.",
            },
            {
                "id": "ev_3",
                "kind": "gemini_event_candidate",
                "source_video_id": "video_000001",
                "text": "People are possibly on a tour in Alaska.",
            },
            {
                "id": "ev_place_1",
                "kind": "gemini_place_candidate",
                "source_video_id": "video_000001",
                "start_s": 190,
                "end_s": 191,
                "text": "а также на Аляску.",
                "metadata": {"name": "Alaska", "evidence_text": "а также на Аляску."},
            },
            {
                "id": "ev_place_2",
                "kind": "gemini_place_candidate",
                "source_video_id": "video_000001",
                "start_s": 280,
                "end_s": 281,
                "text": "В Анкоридже.",
                "metadata": {"name": "Anchorage", "evidence_text": "В Анкоридже."},
            },
        ],
    )

    place_role_result = build_place_roles_for_project(tmp_path)
    assert place_role_result["by_role"]["travel_plan"] == 1
    assert place_role_result["by_role"]["ambiguous_place_reference"] == 2

    build_project_groups(tmp_path)

    roles = read_jsonl(tmp_path / "event_place_roles.jsonl")
    alaska_plan = next(row for row in roles if row["canonical_event_id"] == "canonical_event_000002" and row["label"] == "Alaska")
    anchorage = next(row for row in roles if row["label"] == "Anchorage")
    assert alaska_plan["include_in_place_groups"] is False
    assert alaska_plan["role"] == "travel_plan"
    assert anchorage["include_in_place_groups"] is False
    assert anchorage["role"] == "ambiguous_place_reference"

    places = read_jsonl(tmp_path / "place_groups.jsonl")
    assert "Alaska" not in {place["label"] for place in places}
    assert "Anchorage" not in {place["label"] for place in places}
    madison_park = next(place for place in places if place["label"] == "park")
    assert "Madison, Wisconsin, USA" in madison_park["parent_place_labels"]

    albums = read_jsonl(tmp_path / "albums.jsonl")
    assert all("Alaska" not in album.get("place_label", "") for album in albums)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

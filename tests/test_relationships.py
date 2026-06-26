import json
from pathlib import Path

from tapesplit.relationships import build_relationship_candidates
from tapesplit.storage import read_jsonl


def test_build_relationship_candidates_uses_event_subject_when_name_is_not_in_segment(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Tooth Extraction",
                "start_s": 100,
                "end_s": 160,
                "metadata": {
                    "people": ["Philip"],
                    "event_type": "medical",
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip / Philip",
                "aliases": ["Filip", "Philip"],
                "metadata": {"normalized_key": "filip"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 110,
                "end_s": 114,
                "text": "Ну-ка, покажи маме. Покажи маме.",
                "language": "ru",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "evidence.jsonl",
        [
            {
                "id": "ev_000001",
                "kind": "local_transcript_segment",
                "metadata": {"transcript_segment_id": "tr_000001"},
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 1
    candidates = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert candidates[0]["predicate"] == "mother_candidate"
    assert candidates[0]["subject_label"] == "Unresolved mother"
    assert candidates[0]["object_entity_id"] == "people_group_000001"
    assert candidates[0]["object_label"] == "Filip / Philip"
    assert candidates[0]["metadata"]["context_source"] == "event_subject"
    assert candidates[0]["evidence_ids"] == ["ev_000001"]

    tasks = read_jsonl(tmp_path / "relationship_review_tasks.jsonl")
    assert tasks[0]["candidate_ids"] == ["relationship_candidate_000001"]
    assert "mother" in tasks[0]["question"]


def test_build_relationship_candidates_uses_nearby_name_mentions(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday",
                "start_s": 0,
                "end_s": 80,
                "metadata": {
                    "people": ["Philip", "Galina"],
                    "event_type": "birthday",
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Philip",
                "aliases": ["Philip"],
                "metadata": {"normalized_key": "philip"},
            },
            {
                "id": "people_group_000002",
                "label": "Galina",
                "aliases": ["Galina"],
                "metadata": {"normalized_key": "vera"},
            },
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 20,
                "end_s": 22,
                "text": "Philip, wave to grandma.",
                "language": "en",
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 1
    candidates = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert candidates[0]["predicate"] == "grandparent_candidate"
    assert candidates[0]["object_entity_id"] == "people_group_000001"
    assert candidates[0]["metadata"]["context_source"] == "name_mention"
    assert candidates[0]["confidence"] >= 0.7


def test_build_relationship_candidates_matches_cyrillic_mentions_after_alias_merge(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday",
                "start_s": 0,
                "end_s": 80,
                "metadata": {"people": ["Filipp"], "event_type": "birthday"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip / Filipp / Filya",
                "aliases": ["Filip", "Filipp", "Filya"],
                "metadata": {"normalized_key": "filipp"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 20,
                "end_s": 22,
                "text": "Филипп, скажи бабушке привет.",
                "language": "ru",
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 1
    candidates = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert candidates[0]["object_entity_id"] == "people_group_000001"
    assert candidates[0]["metadata"]["context_source"] == "name_mention"


def test_build_relationship_candidates_does_not_guess_without_person_context(tmp_path: Path):
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 20,
                "end_s": 22,
                "text": "Grandma is here.",
                "language": "en",
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 0
    assert read_jsonl(tmp_path / "relationship_candidates.jsonl") == []


def test_build_relationship_candidates_reads_transcript_evidence_rows(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Tooth Extraction",
                "start_s": 100,
                "end_s": 160,
                "metadata": {"people": ["Philip"]},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Philip",
                "aliases": ["Philip"],
                "metadata": {"normalized_key": "philip"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "evidence.jsonl",
        [
            {
                "id": "ev_000001",
                "source_video_id": "video_000001",
                "start_s": 110,
                "end_s": 114,
                "modality": "transcript",
                "kind": "twelvelabs_search_transcript",
                "text": "Ну-ка, покажи маме.",
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 1
    candidates = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert candidates[0]["evidence_ids"] == ["ev_000001"]
    assert candidates[0]["metadata"]["transcript_segment_ids"] == ["tr_obs_ev_000001"]


def test_build_relationship_candidates_uses_source_local_ranges(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Birthday",
                "start_s": 1000,
                "end_s": 1100,
                "metadata": {
                    "people": ["Philip"],
                    "source_ranges": [
                        {"source_video_id": "video_000002", "start_s": 10, "end_s": 40}
                    ],
                    "source_video_ids": ["video_000002"],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Philip",
                "aliases": ["Philip"],
                "metadata": {"normalized_key": "philip"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000002",
                "start_s": 20,
                "end_s": 22,
                "text": "Philip, wave to grandma.",
                "language": "en",
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 1
    candidates = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert candidates[0]["scope"]["canonical_event_ids"] == ["canonical_event_000001"]


def test_build_relationship_candidates_skips_unanchored_transcript_dialogue(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Family birthday",
                "start_s": 100,
                "end_s": 120,
                "metadata": {"people": ["Philip"]},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Philip",
                "aliases": ["Philip"],
                "metadata": {"normalized_key": "philip"},
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
                "language": "en",
            }
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 0
    assert read_jsonl(tmp_path / "relationship_candidates.jsonl") == []


def test_build_relationship_candidates_extracts_named_parent_from_event_summary(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Playground",
                "summary": "The video shows Katya, Filip's mother, outside a kindergarten.",
                "start_s": 100,
                "end_s": 160,
                "evidence_ids": ["gem_ev_000001"],
                "metadata": {
                    "people": ["Filip", "Katya"],
                    "source_video_ids": ["video_000001"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 10, "end_s": 70}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip / Filipp",
                "aliases": ["Filip", "Filipp"],
                "metadata": {"normalized_key": "filip"},
            },
            {
                "id": "people_group_000002",
                "label": "Ekaterina / Katya",
                "aliases": ["Ekaterina", "Katya"],
                "metadata": {"normalized_key": "ekaterina"},
            },
        ],
    )

    result = build_relationship_candidates(tmp_path)

    assert result["relationship_candidates"] == 1
    candidates = read_jsonl(tmp_path / "relationship_candidates.jsonl")
    assert candidates[0]["predicate"] == "mother_candidate"
    assert candidates[0]["subject_entity_id"] == "people_group_000002"
    assert candidates[0]["object_entity_id"] == "people_group_000001"
    assert candidates[0]["metadata"]["context_source"] == "event_summary_direct_phrase"
    assert candidates[0]["scope"]["start_s"] == 10


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

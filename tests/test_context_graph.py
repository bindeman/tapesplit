import json
from pathlib import Path

from tapesplit.context_graph import build_context_graph
from tapesplit.storage import read_jsonl


def test_build_context_graph_writes_edges_and_metrics(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "First Day at Maplewood",
                "start_s": 10,
                "end_s": 100,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "metadata": {"event_type": "school"},
            },
            {
                "id": "canonical_event_000002",
                "title": "Tooth Extraction",
                "start_s": 120,
                "end_s": 180,
                "confidence": 0.8,
                "evidence_ids": ["ev_2"],
                "metadata": {"event_type": "home"},
            },
        ],
    )
    _write_jsonl(
        tmp_path / "evidence.jsonl",
        [
            {
                "id": "ev_1",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 100,
            },
            {
                "id": "ev_2",
                "source_video_id": "video_000002",
                "start_s": 120,
                "end_s": 180,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Philip",
                "canonical_event_ids": ["canonical_event_000001", "canonical_event_000002"],
                "confidence": 0.85,
            },
            {
                "id": "people_group_000002",
                "label": "Emily",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.75,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "place_groups.jsonl",
        [
            {
                "id": "place_group_000001",
                "label": "Maplewood School",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.9,
            },
            {
                "id": "place_group_000002",
                "label": "Living Room",
                "canonical_event_ids": ["canonical_event_000002"],
                "confidence": 0.7,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [
            {
                "id": "date_group_000001",
                "label": "SEP 7 2005",
                "date_value": "2005-09-07",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.9,
                "excluded_as_event_date": False,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "event_groups.jsonl",
        [
            {
                "id": "event_group_000001",
                "title": "Sep 7 Context",
                "group_type": "same_day_candidate",
                "canonical_event_ids": ["canonical_event_000001", "canonical_event_000002"],
                "confidence": 0.7,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [
            {
                "id": "relationship_candidate_000001",
                "subject_entity_id": "role_entity_mother_of_people_group_000001",
                "subject_label": "Unresolved mother",
                "predicate": "mother_candidate",
                "object_entity_id": "people_group_000001",
                "object_label": "Philip",
                "confidence": 0.58,
                "evidence_ids": ["ev_2"],
                "scope": {"canonical_event_ids": ["canonical_event_000002"]},
            }
        ],
    )

    result = build_context_graph(tmp_path)

    assert result["context_edges"] > 0
    assert result["by_predicate"]["appears_with"] == 1
    assert result["by_predicate"]["appears_in_event"] == 3
    assert result["by_predicate"]["event_at_place"] == 2
    assert result["by_predicate"]["event_date_context"] == 1
    assert result["by_predicate"]["same_date_context"] == 2
    assert result["by_predicate"]["relationship_candidate_edge"] == 1

    edges = read_jsonl(tmp_path / "context_edges.jsonl")
    appears_with = next(edge for edge in edges if edge["predicate"] == "appears_with")
    assert appears_with["subject_label"] == "Philip"
    assert appears_with["object_label"] == "Emily"
    assert appears_with["scope"]["canonical_event_ids"] == ["canonical_event_000001"]
    assert appears_with["scope"]["source_video_ids"] == ["video_000001"]

    relationship = next(edge for edge in edges if edge["predicate"] == "relationship_candidate_edge")
    assert relationship["subject_label"] == "Unresolved mother"
    assert relationship["object_label"] == "Philip"
    assert relationship["metadata"]["relationship_candidate_id"] == "relationship_candidate_000001"
    assert relationship["scope"]["source_video_ids"] == ["video_000002"]

    metrics = read_jsonl(tmp_path / "edge_metrics.jsonl")
    assert len(metrics) == len(edges)
    assert all("computed_weight" in metric for metric in metrics)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

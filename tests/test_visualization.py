import json
from pathlib import Path

from tapesplit.visualization import export_visualization_data


def test_export_visualization_data_builds_ui_ready_timeline_and_graph(tmp_path: Path):
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [
            {
                "id": "video_000001",
                "filename": "tape.mp4",
                "relative_path": "tape.mp4",
                "probe": {"duration_s": 100, "video": {"width": 704, "height": 480}},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "scenes.jsonl",
        [
            {
                "id": "scene_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 50,
                "scene_type": "content",
                "label": "content",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "source_video_id": "video_000001",
                "start_s": 5,
                "end_s": 40,
                "title": "Kitchen Birthday",
                "summary": "A child has a birthday at home.",
                "confidence": 0.9,
                "relatedness": "likely_family",
                "metadata": {"event_type": "birthday"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip",
                "aliases": ["Filip", "Philip"],
                "kind": "named_person_candidate",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.8,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "place_groups.jsonl",
        [
            {
                "id": "place_group_000001",
                "label": "home",
                "kind": "generic_place_context",
                "place_type": "residence",
                "scope_label": "Madison, Wisconsin context",
                "parent_place_labels": ["Madison, Wisconsin"],
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.7,
            },
            {
                "id": "place_group_000002",
                "label": "Madison, Wisconsin",
                "kind": "named_place_candidate",
                "place_type": "region",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.8,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "date_groups.jsonl",
        [
            {
                "id": "date_group_000001",
                "label": "Sep 7, 2005",
                "date_value": "2005-09-07",
                "precision": "day",
                "canonical_event_ids": ["canonical_event_000001"],
            }
        ],
    )
    _write_jsonl(
        tmp_path / "visual_assets.jsonl",
        [
            {
                "id": "visual_asset_000001",
                "subject_type": "event",
                "subject_id": "canonical_event_000001",
                "thumbnail_path": "thumbnails/events/canonical_event_000001.jpg",
                "keyframe_path": "keyframes/events/canonical_event_000001.jpg",
            },
            {
                "id": "visual_asset_000002",
                "subject_type": "scene",
                "subject_id": "scene_000001",
                "thumbnail_path": "thumbnails/scenes/scene_000001.jpg",
                "keyframe_path": "keyframes/scenes/scene_000001.jpg",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "face_observations.jsonl",
        [
            {
                "id": "face_observation_000001",
                "face_cluster_id": "face_cluster_000001",
                "person_group_id": "people_group_000001",
                "face_thumbnail_path": "thumbnails/faces/filip.jpg",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "face_clusters.jsonl",
        [
            {
                "id": "face_cluster_000001",
                "face_count": 1,
                "thumbnail_path": "thumbnails/faces/filip.jpg",
                "candidate_people": [
                    {
                        "person_group_id": "people_group_000001",
                        "person_label": "Filip",
                        "confidence": 0.68,
                    }
                ],
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "face_identity_candidates.jsonl",
        [
            {
                "id": "face_identity_candidate_000001",
                "face_cluster_id": "face_cluster_000001",
                "person_group_id": "people_group_000001",
                "person_label": "Filip",
                "confidence": 0.68,
                "review_status": "needs_review",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "context_edges.jsonl",
        [
            {
                "id": "context_edge_000001",
                "subject_entity_id": "people_group_000001",
                "subject_type": "person",
                "subject_label": "Filip",
                "predicate": "person_place_context",
                "object_entity_id": "place_group_000001",
                "object_type": "place",
                "object_label": "home",
                "confidence": 0.8,
                "scope": {"canonical_event_ids": ["canonical_event_000001"], "source_video_ids": ["video_000001"]},
            },
            {
                "id": "context_edge_000002",
                "subject_entity_id": "place_group_000001",
                "subject_type": "place",
                "subject_label": "home",
                "predicate": "within_region_candidate",
                "object_entity_id": "place_group_000002",
                "object_type": "place",
                "object_label": "Madison, Wisconsin",
                "confidence": 0.7,
                "scope": {"canonical_event_ids": ["canonical_event_000001"], "source_video_ids": ["video_000001"]},
                "metadata": {"basis": ["same_event_place_context"], "not_exportable_as_gps": True},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "edge_metrics.jsonl",
        [{"id": "edge_metric_000001", "edge_id": "context_edge_000001", "computed_weight": 0.62}],
    )

    result = export_visualization_data(tmp_path)
    payload = json.loads((tmp_path / "visualization.json").read_text(encoding="utf-8"))

    assert result["events"] == 1
    assert payload["timeline"]["events"][0]["people"][0]["label"] == "Filip"
    assert payload["timeline"]["events"][0]["places"][0]["label"] == "home (Madison, Wisconsin context)"
    assert payload["tracks"]["people"][0]["thumbnail_path"] == "thumbnails/faces/filip.jpg"
    assert payload["tracks"]["people"][0]["candidate_face_clusters"][0]["face_cluster_id"] == "face_cluster_000001"
    assert payload["tracks"]["places"][0]["display_label"] == "home (Madison, Wisconsin context)"
    assert payload["tracks"]["places"][0]["context"]["label"] == "Madison, Wisconsin context"
    assert payload["place_contexts"][0]["label"] == "Madison, Wisconsin context"
    assert {place["id"] for place in payload["place_contexts"][0]["places"]} == {
        "place_group_000001",
        "place_group_000002",
    }
    assert payload["relationships"]["edges"][0]["weight"] == 0.62
    assert payload["relationships"]["place_context_edges"][0]["source_label"] == "home (Madison, Wisconsin context)"
    assert payload["relationships"]["face_identity_candidates"][0]["person_label"] == "Filip"
    assert payload["review_queue"][0]["task_type"] == "confirm_face_identity"
    assert {item["task_type"] for item in payload["review_queue"]} == {
        "confirm_face_identity",
        "confirm_place_context",
    }
    assert payload["summary"]["review_items"] == 2
    assert payload["assets"]["face_clusters"][0]["id"] == "face_cluster_000001"
    assert payload["assets"]["by_subject"]["event:canonical_event_000001"][0]["thumbnail_path"]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

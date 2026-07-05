"""Event re-grounding: find where mislocated events actually live."""

import json
from pathlib import Path

from tapesplit.regrounding import build_event_regroundings
from tapesplit.review_actions import apply_review_actions
from tapesplit.storage import read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    # Event claims 100-160s but its content (matching the query vector) is
    # actually at 500-560s.
    _write_jsonl(
        project / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Filip Loses a Tooth",
                "summary": "The loose tooth comes out at home.",
                "relatedness": "likely_family",
                "review_status": "unreviewed",
                "metadata": {
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 100.0, "end_s": 160.0}]
                },
            }
        ],
    )
    _write_jsonl(
        project / "event_alignments.jsonl",
        [
            {
                "canonical_event_id": "canonical_event_000001",
                "timing_status": "model_only",
                "support_score": 0.2,
            }
        ],
    )
    target = [1.0, 0.0, 0.0]
    off = [0.0, 1.0, 0.0]
    scenes = []
    for index, time_s in enumerate(range(0, 700, 20)):
        vector = target if 500 <= time_s <= 560 else off
        scenes.append(
            {
                "id": f"emb_{index:04d}",
                "visual_asset_id": f"asset_{index:04d}",
                "source_subject_type": "scene",
                "source_video_id": "video_000001",
                "time_s": float(time_s),
                "start_s": float(time_s),
                "end_s": float(time_s + 20),
                "vector": vector,
            }
        )
    _write_jsonl(project / "visual_embeddings.jsonl", scenes)
    _write_jsonl(
        project / "visual_captions.jsonl",
        [{"visual_asset_id": "asset_0025", "caption": "a child showing a loose tooth at home"}],
    )
    _write_jsonl(
        project / "transcript_segments.jsonl",
        [
            {
                "source_video_id": "video_000001",
                "start_s": 520.0,
                "end_s": 526.0,
                "text": "look the tooth is loose it came out",
            }
        ],
    )
    return project


def test_regrounding_proposes_matching_window(tmp_path):
    project = _setup_project(tmp_path)

    result = build_event_regroundings(
        project,
        encode_text=lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
    )

    assert result["proposals"] == 1
    proposal = read_jsonl(project / "event_regroundings.jsonl")[0]
    assert proposal["canonical_event_id"] == "canonical_event_000001"
    assert 480 <= proposal["proposed_start_s"] <= 520
    assert 540 <= proposal["proposed_end_s"] <= 600
    assert proposal["transcript_corroborated"] is True
    assert proposal["confidence"] > 0.6


def test_regrounding_skips_already_grounded_events(tmp_path):
    project = _setup_project(tmp_path)
    # Query matches the CLAIMED range instead → no proposal.
    scenes = read_jsonl(project / "visual_embeddings.jsonl")
    for scene in scenes:
        scene["vector"] = [1.0, 0.0, 0.0] if 100 <= scene["time_s"] <= 160 else [0.0, 1.0, 0.0]
    _write_jsonl(project / "visual_embeddings.jsonl", scenes)

    result = build_event_regroundings(
        project,
        encode_text=lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
    )
    assert result["proposals"] == 0
    assert result["skipped"]["already_grounded"] == 1


def test_move_event_range_action_applies_durably(tmp_path):
    project = _setup_project(tmp_path)
    apply_review_actions(
        project,
        actions=[
            {
                "action": "move_event_range",
                "target_id": "canonical_event_000001",
                "target_type": "event",
                "reviewer": "phillip",
                "payload": {"source_video_id": "video_000001", "start_s": 500.0, "end_s": 560.0},
            }
        ],
    )
    event = read_jsonl(project / "canonical_events.jsonl")[0]
    assert event["metadata"]["source_ranges"] == [
        {"source_video_id": "video_000001", "start_s": 500.0, "end_s": 560.0}
    ]
    assert event["metadata"]["regrounded"] is True
    assert event["metadata"]["pre_regrounding_source_ranges"][0]["start_s"] == 100.0
    assert event["review_status"] == "confirmed"
    corrections = read_jsonl(project / "corrections.jsonl")
    assert corrections[0]["action"] == "move_event_range"

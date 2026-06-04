import csv
import json
import sqlite3
from pathlib import Path

from tapesplit.cli import main
from tapesplit.evaluation import build_eval_packet, score_eval_packet
from tapesplit.storage import read_jsonl


def test_build_eval_packet_writes_reviewer_artifacts_and_filters_excluded_context(tmp_path: Path):
    _write_eval_project(tmp_path)

    result = build_eval_packet(tmp_path, max_items=0)
    packet = tmp_path / "eval_packet"

    assert result["items"] == 7
    assert result["by_task_type"] == {
        "album_grouping": 1,
        "context_edge": 1,
        "date_candidate": 1,
        "event_correctness": 1,
        "person_identity": 1,
        "place_resolution": 1,
        "relationship_candidate": 1,
    }
    for filename in [
        "README.md",
        "annotations.template.csv",
        "annotations.template.jsonl",
        "eval.sqlite",
        "eval_items.csv",
        "eval_items.jsonl",
        "eval_manifest.json",
    ]:
        assert (packet / filename).exists()

    labels = "\n".join(item["predicted_label"] for item in read_jsonl(packet / "eval_items.jsonl"))
    assert "Filip Birthday" in labels
    assert "Filip" in labels
    assert "Wonderland" not in labels
    assert "Alice" not in labels

    conn = sqlite3.connect(packet / "eval.sqlite")
    try:
        task_counts = dict(conn.execute("SELECT task_type, item_count FROM task_counts"))
        followup_count = conn.execute("SELECT COUNT(*) FROM followup_queue").fetchone()[0]
        prompt_count = conn.execute("SELECT COUNT(*) FROM prompting_lab").fetchone()[0]
    finally:
        conn.close()
    assert task_counts["event_correctness"] == 1
    assert followup_count >= 3
    assert prompt_count == 7


def test_score_eval_packet_accepts_csv_and_writes_followup_outputs(tmp_path: Path):
    _write_eval_project(tmp_path)
    build_eval_packet(tmp_path, max_items=0)
    packet = tmp_path / "eval_packet"
    items = read_jsonl(packet / "eval_items.jsonl")
    _write_annotations_csv(
        packet / "annotations.csv",
        [
            {
                "eval_item_id": items[0]["id"],
                "judgment": "correct",
                "corrected_value": "",
                "needs_family_followup": "false",
                "family_followup_question": "",
                "notes": "Looks right.",
                "reviewer": "brother",
                "reviewed_at": "2026-06-04",
            },
            {
                "eval_item_id": items[1]["id"],
                "judgment": "needs_followup",
                "corrected_value": "",
                "needs_family_followup": "true",
                "family_followup_question": "Ask Dad if this was at the apartment.",
                "notes": "Recognizable but uncertain.",
                "reviewer": "brother",
                "reviewed_at": "2026-06-04",
            },
            {
                "eval_item_id": items[2]["id"],
                "judgment": "partial",
                "corrected_value": "Home, exact city unknown",
                "needs_family_followup": "no",
                "family_followup_question": "",
                "notes": "Home is right, city is unknown.",
                "reviewer": "brother",
                "reviewed_at": "2026-06-04",
            },
        ],
    )

    report = score_eval_packet(tmp_path)

    assert report["matched_annotations"] == 3
    assert report["overall"]["scored"] == 2
    assert report["overall"]["score"] == 0.75
    assert report["followup_items"] == 1
    assert (packet / "eval_report.json").exists()
    assert (packet / "scored_items.jsonl").exists()
    assert (packet / "family_followups.jsonl").exists()
    followups = read_jsonl(packet / "family_followups.jsonl")
    assert followups[0]["family_followup_question"] == "Ask Dad if this was at the apartment."

    conn = sqlite3.connect(packet / "eval.sqlite")
    try:
        scored_count = conn.execute("SELECT COUNT(*) FROM scored_annotations").fetchone()[0]
        overall = conn.execute("SELECT score FROM score_summary WHERE scope = 'overall'").fetchone()[0]
    finally:
        conn.close()
    assert scored_count == 3
    assert overall == 0.75


def test_eval_cli_build_command(tmp_path: Path, capsys):
    _write_eval_project(tmp_path)

    assert main(["eval", "build", str(tmp_path), "--max-items", "2", "--force"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["items"] == 2
    assert (tmp_path / "eval_packet" / "eval_items.jsonl").exists()


def _write_eval_project(project: Path) -> None:
    _write_jsonl(
        project / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Filip Birthday",
                "summary": "Filip blows out candles at home.",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 120,
                "confidence": 0.92,
                "relatedness": "likely_family",
                "evidence_ids": ["ev_family"],
                "metadata": {
                    "event_type": "birthday",
                    "people": ["Filip"],
                    "place_candidates": ["Home"],
                    "date_candidates": ["SEP 7 2005"],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "Alice Movie",
                "summary": "Unrelated broadcast content.",
                "source_video_id": "video_000001",
                "start_s": 1000,
                "end_s": 1100,
                "confidence": 0.95,
                "relatedness": "likely_unrelated",
                "evidence_ids": ["ev_unrelated"],
                "metadata": {
                    "event_type": "movie",
                    "people": ["Alice"],
                    "place_candidates": ["Wonderland"],
                },
            },
        ],
    )
    _write_jsonl(
        project / "evidence.jsonl",
        [
            {
                "id": "ev_family",
                "kind": "visual_summary",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 120,
                "text": "Child birthday party at home.",
            },
            {
                "id": "ev_unrelated",
                "kind": "visual_summary",
                "source_video_id": "video_000001",
                "start_s": 1000,
                "end_s": 1100,
                "text": "Alice in Wonderland broadcast.",
            },
        ],
    )
    _write_jsonl(
        project / "albums.jsonl",
        [
            {
                "id": "album_000001",
                "title": "Filip Birthday",
                "album_type": "event",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.9,
                "review_status": "unreviewed",
            },
            {
                "id": "album_000002",
                "title": "Alice Movie",
                "album_type": "unrelated_content",
                "export_status": "excluded",
                "canonical_event_ids": ["canonical_event_000002"],
                "confidence": 0.9,
            },
        ],
    )
    _write_jsonl(
        project / "place_groups.jsonl",
        [
            {
                "id": "place_group_000001",
                "label": "Home",
                "kind": "generic_place_context",
                "place_type": "residence",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.7,
                "review_status": "needs_review",
            },
            {
                "id": "place_group_000002",
                "label": "Wonderland",
                "kind": "named_place_candidate",
                "canonical_event_ids": ["canonical_event_000002"],
                "confidence": 0.8,
            },
        ],
    )
    _write_jsonl(
        project / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip",
                "kind": "named_person_candidate",
                "aliases": ["Filip", "Philip"],
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.8,
                "review_status": "needs_review",
            },
            {
                "id": "people_group_000002",
                "label": "Alice",
                "kind": "fictional_person",
                "canonical_event_ids": ["canonical_event_000002"],
                "confidence": 0.8,
            },
        ],
    )
    _write_jsonl(
        project / "date_groups.jsonl",
        [
            {
                "id": "date_group_000001",
                "label": "SEP 7 2005",
                "date_value": "2005-09-07",
                "precision": "day",
                "source_kind": "event_date_candidate",
                "canonical_event_ids": ["canonical_event_000001"],
                "confidence": 0.85,
            }
        ],
    )
    _write_jsonl(
        project / "relationship_candidates.jsonl",
        [
            {
                "id": "relationship_candidate_000001",
                "subject_label": "Ekaterina",
                "predicate": "mother_candidate",
                "object_label": "Filip",
                "confidence": 0.6,
                "supporting_signals": ["adult says my son"],
                "evidence_ids": ["ev_family"],
                "scope": {"canonical_event_ids": ["canonical_event_000001"]},
            }
        ],
    )
    _write_jsonl(
        project / "context_edges.jsonl",
        [
            {
                "id": "context_edge_000001",
                "subject_label": "Kitchen",
                "predicate": "inside_place_candidate",
                "object_label": "Home",
                "confidence": 0.72,
                "review_status": "needs_review",
                "supporting_signals": ["same birthday scene"],
                "scope": {"canonical_event_ids": ["canonical_event_000001"]},
            },
            {
                "id": "context_edge_000002",
                "subject_label": "Alice",
                "predicate": "inside_place_candidate",
                "object_label": "Wonderland",
                "confidence": 0.9,
                "review_status": "needs_review",
                "supporting_signals": ["broadcast scene"],
                "scope": {"canonical_event_ids": ["canonical_event_000002"]},
            },
        ],
    )


def _write_annotations_csv(path: Path, rows: list[dict]) -> None:
    columns = [
        "eval_item_id",
        "judgment",
        "corrected_value",
        "needs_family_followup",
        "family_followup_question",
        "notes",
        "reviewer",
        "reviewed_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

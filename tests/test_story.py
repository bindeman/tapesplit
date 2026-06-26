import json
from pathlib import Path

from tapesplit.story import export_story


def test_export_story_uses_reconciled_event_context(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Alaska Trip",
                "summary": "Original model summary.",
                "start_s": 120,
                "end_s": 180,
                "confidence": 0.8,
                "review_status": "unreviewed",
                "relatedness": "likely_family",
                "metadata": {"event_type": "travel", "source_video_ids": ["video_000001"]},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "albums.jsonl",
        [
            {
                "id": "album_000001",
                "title": "Jun 25, 2002",
                "album_type": "day",
                "canonical_event_ids": ["canonical_event_000001"],
                "date_label": "JUN 25 2002",
                "place_label": "Whitewater",
                "people_labels": ["Filip"],
                "language_labels": ["Russian"],
                "source_video_ids": ["video_000001"],
                "start_s": 120,
                "end_s": 180,
                "confidence": 0.8,
                "review_status": "unreviewed",
                "export_status": "candidate",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "event_reconciliations.jsonl",
        [
            {
                "id": "event_reconciliation_000001",
                "canonical_event_id": "canonical_event_000001",
                "reconciled_title": "Whitewater Farmhouse Visit",
                "reconciled_summary": "Appears to be filmed around Whitewater.",
                "reconciliation_status": "corrected",
                "selected_place_labels": ["Whitewater"],
                "rejected_place_labels": ["Alaska"],
            }
        ],
    )

    result = export_story(tmp_path)
    story = json.loads((tmp_path / "story.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "tape_story.md").read_text(encoding="utf-8")

    assert result["chapters"] == 1
    assert story["chapters"][0]["events"][0]["title"] == "Whitewater Farmhouse Visit"
    assert story["chapters"][0]["events"][0]["rejected_place_labels"] == ["Alaska"]
    assert "Whitewater Farmhouse Visit" in markdown
    assert "Appears to be filmed around Whitewater." in markdown


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

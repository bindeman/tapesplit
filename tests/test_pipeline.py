import json
from pathlib import Path

from tapesplit.pipeline import rebuild_project_outputs
from tapesplit.storage import read_jsonl


def test_rebuild_reapplies_event_corrections_before_classification(tmp_path: Path, monkeypatch):
    _write_jsonl(
        tmp_path / "corrections.jsonl",
        [
            {
                "id": "correction_000001",
                "action": "mark_unrelated",
                "target_type": "event",
                "target_id": "canonical_event_000001",
                "reviewed_at": "2026-01-01T00:00:00+00:00",
                "reviewer": "test",
                "notes": "",
                "payload": {},
            }
        ],
    )
    seen_relatedness = []

    def fake_stitch(project: Path, **_kwargs):
        _write_jsonl(
            project / "canonical_events.jsonl",
            [
                {
                    "id": "canonical_event_000001",
                    "title": "Movie clip",
                    "source_video_id": "video_000001",
                    "start_s": 0,
                    "end_s": 10,
                    "relatedness": "likely_family",
                }
            ],
        )
        return {"canonical_events": 1}

    def fake_classify(project: Path):
        event = read_jsonl(project / "canonical_events.jsonl")[0]
        seen_relatedness.append(event.get("relatedness"))
        return {"content_classifications": 1}

    monkeypatch.setattr("tapesplit.pipeline.build_evidence", lambda project: {"evidence_count": 0})
    monkeypatch.setattr("tapesplit.pipeline.stitch_project_events", fake_stitch)
    monkeypatch.setattr("tapesplit.pipeline.build_content_classifications_for_project", fake_classify)
    monkeypatch.setattr("tapesplit.pipeline.build_project_groups", lambda project, **_kwargs: {})
    monkeypatch.setattr("tapesplit.pipeline.build_place_roles_for_project", lambda project, **_kwargs: {})
    monkeypatch.setattr("tapesplit.pipeline.build_event_alignments", lambda project, **_kwargs: {})
    monkeypatch.setattr("tapesplit.pipeline.build_event_reconciliations", lambda project: {})
    monkeypatch.setattr("tapesplit.pipeline.build_relationship_candidates", lambda project, **_kwargs: {})
    monkeypatch.setattr("tapesplit.pipeline.build_context_graph", lambda project: {})
    monkeypatch.setattr("tapesplit.pipeline.build_search_index", lambda project, **_kwargs: {})

    result = rebuild_project_outputs(
        tmp_path,
        export_report=False,
        export_story_output=False,
        export_visualization=False,
    )
    event = read_jsonl(tmp_path / "canonical_events.jsonl")[0]

    assert "review_reapply_after_stitch_events" in [step["step"] for step in result["steps"]]
    assert seen_relatedness == ["likely_unrelated"]
    assert event["relatedness"] == "likely_unrelated"
    assert event["review_status"] == "excluded"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

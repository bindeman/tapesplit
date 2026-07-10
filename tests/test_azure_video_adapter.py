"""Scene-guided second-opinion adapter: planning, filtering, adjudication."""

import json
from pathlib import Path

from tapesplit.azure_video_adapter import (
    MAX_IMAGES_PER_REQUEST,
    SceneWindow,
    _window_prompt,
    adjudicate_against_canonical,
    analyze_window,
    plan_scene_windows,
)
from tapesplit.storage import append_jsonl


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    return project


def test_plan_scene_windows_batches_by_count_and_span(tmp_path: Path):
    project = _project(tmp_path)
    # 100 content scenes of 10s each -> span cap (720s) bites before count cap
    for i in range(100):
        append_jsonl(
            project / "scenes.jsonl",
            {
                "id": f"scene_{i:06d}",
                "source_video_id": "video_000001",
                "start_s": i * 10.0,
                "end_s": i * 10.0 + 9.0,
                "kind": "content",
            },
        )
    append_jsonl(
        project / "scenes.jsonl",
        {
            "id": "scene_nc",
            "source_video_id": "video_000001",
            "start_s": 990.0,
            "end_s": 995.0,
            "kind": "non_content",
        },
    )
    windows = plan_scene_windows(project, "video_000001")
    assert len(windows) >= 2
    assert all(len(w.scene_mids) <= MAX_IMAGES_PER_REQUEST for w in windows)
    assert all(w.end_s - w.start_s <= 730.0 for w in windows)
    # windows tile the content scenes in order without losing any
    total = sum(len(w.scene_mids) for w in windows)
    assert total == 100  # non_content excluded
    assert windows[0].index == 0 and windows[1].start_s > windows[0].start_s


def test_window_prompt_pins_absolute_bounds():
    window = SceneWindow("video_000001", 3, 6195.0, 7095.0, (6200.0,))
    prompt = _window_prompt(window)
    assert "[6195, 7095]" in prompt
    assert "ABSOLUTE" in prompt


def test_analyze_window_bisects_content_filter(tmp_path: Path, monkeypatch):
    from tapesplit.azure_openai_adapter import ContentPolicyViolation

    project = _project(tmp_path)
    window = SceneWindow("video_000001", 0, 0.0, 100.0, (10.0, 30.0, 50.0, 70.0))
    frames = [(t, f"jpeg-{t}".encode()) for t in window.scene_mids]
    monkeypatch.setattr(
        "tapesplit.azure_video_adapter.sample_window_frames", lambda _s, _w: list(frames)
    )

    import base64

    flagged = base64.b64encode(b"jpeg-30.0").decode()

    def fake_completion(**kwargs):
        joined = json.dumps(kwargs["messages"][0]["content"])
        if flagged in joined:
            raise ContentPolicyViolation("content_policy_violation")
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "window_summary": "ok",
                                "event_candidates": [
                                    {"title": "Party", "start_s": 12, "end_s": 60, "confidence": 0.8}
                                ],
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }

    result = analyze_window(
        project, tmp_path / "src.mp4", window, completion_fn=fake_completion
    )
    assert result.frames_used == 3
    assert result.frames_dropped == 1
    assert result.analysis["event_candidates"][0]["title"] == "Party"


def test_adjudication_classifies_corroborated_disputed_and_azure_only(tmp_path: Path):
    project = _project(tmp_path)
    append_jsonl(
        project / "azure_analyses.jsonl",
        {
            "source_video_id": "video_000001",
            "deployment": "gpt-5.6-terra",
            "window": {"index": 0, "start_s": 0.0, "end_s": 900.0},
            "analysis": {
                "event_candidates": [
                    {
                        "title": "Backyard birthday party with cake",
                        "summary": "kids singing around a cake",
                        "start_s": 100.0,
                        "end_s": 220.0,
                        "relatedness": "likely_family",
                        "confidence": 0.9,
                    },
                    {
                        "title": "Geology lab tour with mineral cases",
                        "summary": "rock specimens in display cases",
                        "start_s": 500.0,
                        "end_s": 640.0,
                        "relatedness": "likely_family",
                        "confidence": 0.8,
                    },
                    {
                        "title": "Puppy plays in sprinkler",
                        "summary": "dog running through water",
                        "start_s": 700.0,
                        "end_s": 780.0,
                        "relatedness": "likely_family",
                        "confidence": 0.7,
                    },
                ]
            },
        },
    )
    events = [
        # corroborated: overlapping + shared tokens
        {
            "id": "canonical_event_000001",
            "title": "Birthday party in the backyard",
            "summary": "cake and singing",
            "metadata": {
                "source_ranges": [
                    {"source_video_id": "video_000001", "start_s": 90.0, "end_s": 230.0}
                ]
            },
        },
        # range_disputed: azure sees the lab content elsewhere on the tape
        {
            "id": "canonical_event_000002",
            "title": "Geology lab tour",
            "summary": "mineral specimens on display",
            "metadata": {
                "source_ranges": [
                    {"source_video_id": "video_000001", "start_s": 800.0, "end_s": 860.0}
                ]
            },
        },
        # uncovered: outside every analyzed window
        {
            "id": "canonical_event_000003",
            "title": "Beach walk",
            "summary": "",
            "metadata": {
                "source_ranges": [
                    {"source_video_id": "video_000001", "start_s": 2000.0, "end_s": 2100.0}
                ]
            },
        },
    ]
    for event in events:
        append_jsonl(project / "canonical_events.jsonl", event)

    report = adjudicate_against_canonical(project)
    counts = report["counts"]
    assert counts["corroborated"] == 1
    assert counts["range_disputed"] == 1
    assert counts["uncovered"] == 1
    assert counts["azure_only"] == 1  # the sprinkler puppy has no canonical twin
    disputed = report["verdicts"]["range_disputed"][0]
    assert disputed["canonical_event_id"] == "canonical_event_000002"
    assert disputed["azure_range"] == [500.0, 640.0]
    assert (project / "azure_adjudication.json").exists()

from pathlib import Path

from tapesplit.event_stitching import load_source_events, stitch_project_events
from tapesplit.heuristic_events import build_heuristic_events
from tapesplit.storage import append_jsonl, read_jsonl, write_json


def _make_project(tmp_path: Path, *, tapes: list[dict]) -> Path:
    project = tmp_path / "sample.tapesplit"
    project.mkdir()
    write_json(project / "manifest.json", {"schema_version": 1})
    for tape in tapes:
        append_jsonl(project / "tapes.jsonl", tape)
    return project


def _tape(tape_id: str, duration_s: float, filename: str | None = None) -> dict:
    return {
        "id": tape_id,
        "filename": filename or f"{tape_id}.mp4",
        "path": f"/videos/{tape_id}.mp4",
        "probe": {"duration_s": duration_s},
    }


def test_builds_events_from_content_scenes_with_transcript_keywords(tmp_path):
    project = _make_project(tmp_path, tapes=[_tape("video_000001", 120.0)])
    append_jsonl(
        project / "scenes.jsonl",
        {"source_video_id": "video_000001", "scene_type": "non_content", "start_s": 0.0, "end_s": 10.0},
    )
    append_jsonl(
        project / "scenes.jsonl",
        {"source_video_id": "video_000001", "scene_type": "content", "start_s": 10.0, "end_s": 60.0},
    )
    append_jsonl(
        project / "scenes.jsonl",
        {"source_video_id": "video_000001", "scene_type": "content", "start_s": 60.0, "end_s": 110.0},
    )
    append_jsonl(
        project / "transcript_segments.jsonl",
        {
            "source_video_id": "video_000001",
            "start_s": 12.0,
            "end_s": 20.0,
            "language": "en",
            "text": "Happy birthday Anna, blow out the candles for Grandma Rose",
        },
    )

    result = build_heuristic_events(project)

    assert result["heuristic_events"] == 1  # adjacent scenes merge into one span
    events = read_jsonl(project / "heuristic_events.jsonl")
    event = events[0]
    assert event["source"] == "local_heuristic"
    assert event["source_video_id"] == "video_000001"
    assert event["start_s"] == 10.0
    assert event["end_s"] == 110.0
    assert event["confidence"] <= 0.3
    # Proper nouns (names) outrank generic words in the title.
    assert "anna" in event["title"].lower()
    assert "grandma" in event["title"].lower()
    assert "appears to mention" in event["summary"].lower()
    assert event["metadata"]["languages"] == ["en"]
    assert event["metadata"]["heuristic"] is True


def test_falls_back_to_non_content_complement_then_whole_tape(tmp_path):
    project = _make_project(
        tmp_path,
        tapes=[_tape("video_000001", 100.0), _tape("video_000002", 50.0)],
    )
    append_jsonl(
        project / "non_content_ranges.jsonl",
        {"source_video_id": "video_000001", "start_s": 40.0, "end_s": 60.0, "label": "blue"},
    )

    build_heuristic_events(project)
    events = read_jsonl(project / "heuristic_events.jsonl")
    by_source = {}
    for event in events:
        by_source.setdefault(event["source_video_id"], []).append(event)

    spans_one = [(event["start_s"], event["end_s"]) for event in by_source["video_000001"]]
    assert spans_one == [(0.0, 40.0), (60.0, 100.0)]
    spans_two = [(event["start_s"], event["end_s"]) for event in by_source["video_000002"]]
    assert spans_two == [(0.0, 50.0)]
    assert "Recording segment" in by_source["video_000002"][0]["title"]


def test_skips_sources_covered_by_analyzed_events(tmp_path):
    project = _make_project(
        tmp_path,
        tapes=[_tape("video_000001", 100.0), _tape("video_000002", 100.0)],
    )
    append_jsonl(
        project / "gemini_events.jsonl",
        {
            "id": "gem_event_000001",
            "source_video_id": "video_000001",
            "title": "Birthday",
            "start_s": 0.0,
            "end_s": 30.0,
        },
    )

    result = build_heuristic_events(project)

    assert result["sources_skipped_covered"] == ["video_000001"]
    events = read_jsonl(project / "heuristic_events.jsonl")
    assert {event["source_video_id"] for event in events} == {"video_000002"}


def test_short_spans_filtered_but_longest_kept_as_fallback(tmp_path):
    project = _make_project(tmp_path, tapes=[_tape("video_000001", 9.0)])

    build_heuristic_events(project, min_event_seconds=10.0)
    events = read_jsonl(project / "heuristic_events.jsonl")
    assert len(events) == 1
    assert events[0]["end_s"] == 9.0


def test_stitching_uses_heuristic_events_only_for_uncovered_sources(tmp_path):
    project = _make_project(
        tmp_path,
        tapes=[_tape("video_000001", 100.0), _tape("video_000002", 100.0)],
    )
    append_jsonl(
        project / "gemini_events.jsonl",
        {
            "id": "gem_event_000001",
            "source_video_id": "video_000001",
            "title": "School play in spring",
            "start_s": 0.0,
            "end_s": 30.0,
            "confidence": 0.9,
        },
    )
    # Stale heuristic file that includes a source now covered by Gemini.
    append_jsonl(
        project / "heuristic_events.jsonl",
        {
            "id": "heuristic_event_video_000001_0001",
            "source": "local_heuristic",
            "source_video_id": "video_000001",
            "title": "Stale segment",
            "start_s": 0.0,
            "end_s": 90.0,
            "metadata": {"source_video_ids": ["video_000001"]},
        },
    )
    append_jsonl(
        project / "heuristic_events.jsonl",
        {
            "id": "heuristic_event_video_000002_0001",
            "source": "local_heuristic",
            "source_video_id": "video_000002",
            "title": "Lake trip talk",
            "start_s": 5.0,
            "end_s": 80.0,
            "metadata": {"source_video_ids": ["video_000002"]},
        },
    )

    loaded = load_source_events(project)
    ids = {event["id"] for event in loaded}
    assert "gem_event_000001" in ids
    assert "heuristic_event_video_000002_0001" in ids
    assert "heuristic_event_video_000001_0001" not in ids

    result = stitch_project_events(project)
    assert result["canonical_events"] == 2
    canonical = read_jsonl(project / "canonical_events.jsonl")
    titles = {event["title"] for event in canonical}
    assert titles == {"School play in spring", "Lake trip talk"}

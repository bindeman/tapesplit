import json

from tapesplit.gemini_compare import compare_gemini_analysis_modes, summarize_gemini_analyses


def test_summarize_gemini_analyses_counts_chunk_and_whole_tape(tmp_path):
    project = tmp_path / "project.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "tapes.jsonl",
        [{"id": "video_000001", "filename": "one.mp4", "probe": {"duration_s": 120}}],
    )
    _write_jsonl(
        project / "gemini_analyses.jsonl",
        [
            {
                "analysis_run_id": "chunk_run",
                "source_video_id": "video_000001",
                "filename": "one.mp4",
                "time_basis": "chunk",
                "chunk_duration_s": 60,
                "analysis": {
                    "event_candidates": [
                        {
                            "title": "Birthday",
                            "people": ["Filip"],
                            "place_candidates": ["home"],
                            "date_candidates": ["1999"],
                            "relatedness": "likely_family",
                        }
                    ],
                    "person_mentions": [{"name": "Filip"}],
                    "place_candidates": [{"name": "home"}],
                    "date_candidates": [{"value": "1999"}],
                    "followup_segments": [{"reason": "read sign"}],
                },
            },
            {
                "analysis_run_id": "whole_run",
                "source_video_id": "video_000001",
                "filename": "one.mp4",
                "time_basis": "source_video",
                "prepared_video": {"duration_s": 120},
                "analysis": {
                    "event_candidates": [
                        {
                            "title": "Birthday at home",
                            "people": ["Filip", "Ekaterina"],
                            "place_candidates": ["home", "Madison"],
                            "date_candidates": ["1999"],
                            "relatedness": "likely_family",
                        }
                    ],
                    "person_mentions": [{"name": "Ekaterina"}],
                    "place_candidates": [{"name": "Madison"}],
                    "date_candidates": [{"value": "1999"}],
                },
            },
        ],
    )

    summary = summarize_gemini_analyses(project)

    assert [row["mode"] for row in summary["summaries"]] == ["chunk", "whole_tape"]
    assert summary["summaries"][0]["unique_people"] == 1
    assert summary["summaries"][1]["unique_people"] == 2


def test_compare_gemini_analysis_modes_reports_whole_only_entities(tmp_path):
    project = tmp_path / "project.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "tapes.jsonl",
        [{"id": "video_000001", "filename": "one.mp4", "probe": {"duration_s": 120}}],
    )
    _write_jsonl(
        project / "gemini_analyses.jsonl",
        [
            {
                "analysis_run_id": "chunk_run",
                "source_video_id": "video_000001",
                "time_basis": "chunk",
                "chunk_duration_s": 60,
                "analysis": {"event_candidates": [{"people": ["Filip"], "place_candidates": ["home"]}]},
            },
            {
                "analysis_run_id": "whole_run",
                "source_video_id": "video_000001",
                "time_basis": "source_video",
                "analysis": {
                    "event_candidates": [
                        {"people": ["Filip", "Ekaterina"], "place_candidates": ["home", "Madison"]}
                    ]
                },
            },
        ],
    )

    comparison = compare_gemini_analysis_modes(project)

    assert comparison["missing_whole_tape"] == []
    assert comparison["comparisons"][0]["status"] == "compared"
    assert comparison["comparisons"][0]["whole_only_people"] == ["Ekaterina"]
    assert comparison["comparisons"][0]["whole_only_places"] == ["Madison"]


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

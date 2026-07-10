from tapesplit.gemini_import import import_gemini_analysis, _normalize_interval
from tapesplit.storage import read_jsonl


def _write_single_chunk_project(tmp_path, events_json: str, extra_fields: str = "") -> None:
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_000001","probe":{"duration_s":7095}}\n',
        encoding="utf-8",
    )
    (tmp_path / "gemini_analyses.jsonl").write_text(
        (
            '{"analysis_run_id":"run_a","source_video_id":"video_000001",'
            '"chunk_id":"chunk_0008","chunk_index":8,"chunk_start_s":6195,'
            '"chunk_end_s":7095,"chunk_duration_s":900,"time_basis":"chunk",'
            f'"analysis":{{"event_candidates":[{events_json}]{extra_fields}}}}}\n'
        ),
        encoding="utf-8",
    )


def test_normalize_interval_drops_ranges_after_source_duration():
    assert _normalize_interval({"start_s": 120, "end_s": 130}, 100, []) is None


def test_normalize_interval_clamps_to_source_duration():
    assert _normalize_interval({"start_s": 90, "end_s": 130}, 100, []) == (
        90,
        100,
        ["end_clamped_to_source_duration"],
    )


def test_normalize_interval_clamps_before_hard_blue_screen_boundary():
    assert _normalize_interval(
        {"start_s": 90, "end_s": 130},
        200,
        [(100, 180, "blue_screen_no_signal")],
    ) == (90, 100, ["end_clamped_before_blue_screen_no_signal"])


def test_normalize_interval_drops_inside_hard_boundary():
    assert (
        _normalize_interval(
            {"start_s": 110, "end_s": 130},
            200,
            [(100, 180, "blue_screen_no_signal")],
        )
        is None
    )


def test_normalize_interval_offsets_chunk_local_times_before_boundaries():
    assert _normalize_interval(
        {"start_s": 10, "end_s": 20},
        1000,
        [(615, 700, "blue_screen_no_signal")],
        source_offset_s=600,
    ) == (610, 615, ["end_clamped_before_blue_screen_no_signal"])


def test_normalize_interval_missing_chunk_times_use_chunk_offset():
    assert _normalize_interval(
        {},
        1000,
        [],
        source_offset_s=600,
    ) == (600, 600, ["missing_time_range"])


def test_import_chunked_gemini_analysis_offsets_all_selected_chunks(tmp_path):
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_000001","probe":{"duration_s":1000}}\n',
        encoding="utf-8",
    )
    (tmp_path / "non_content_ranges.jsonl").write_text(
        (
            '{"source_video_id":"video_000001","label":"blue_screen_no_signal",'
            '"start_s":650,"end_s":750,"duration_s":100}\n'
        ),
        encoding="utf-8",
    )
    (tmp_path / "gemini_analyses.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"analysis_run_id":"run_a","source_video_id":"video_000001",'
                    '"chunk_id":"chunk_0001","chunk_index":1,"chunk_start_s":0,'
                    '"chunk_end_s":100,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"event_candidates":[{"title":"First","start_s":10,'
                    '"end_s":20,"summary":"first event","confidence":0.8}]}}'
                ),
                (
                    '{"analysis_run_id":"run_b","source_video_id":"video_000001",'
                    '"chunk_id":"chunk_0001","chunk_index":1,"chunk_start_s":600,'
                    '"chunk_end_s":700,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"tape_summary":"chunk summary","event_candidates":['
                    '{"title":"Second","start_s":10,"end_s":20,"summary":"second event",'
                    '"confidence":0.8},'
                    '{"title":"Boundary","start_s":40,"end_s":90,"summary":"crosses boundary",'
                    '"confidence":0.7}]}}'
                ),
                (
                    '{"analysis_run_id":"run_b","source_video_id":"video_000001",'
                    '"chunk_id":"chunk_0002","chunk_index":2,"chunk_start_s":800,'
                    '"chunk_end_s":900,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"event_candidates":[{"title":"Third","start_s":5,'
                    '"end_s":15,"summary":"third event","confidence":0.8}]}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = import_gemini_analysis(tmp_path)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")
    evidence = read_jsonl(tmp_path / "gemini_evidence.jsonl")

    assert result["analysis_run_id"] == "run_b"
    assert result["analyses"] == 2
    assert [(event["title"], event["start_s"], event["end_s"]) for event in events] == [
        ("Second", 610, 620),
        ("Boundary", 640, 650),
        ("Third", 805, 815),
    ]
    assert events[1]["review_status"] == "needs_review"
    assert events[0]["metadata"]["chunk_id"] == "chunk_0001"
    assert evidence[0]["kind"] == "gemini_chunk_summary"


def test_overrun_chunk_timeline_rescaled_and_dropped_events_resurrected(tmp_path):
    # Mirrors the real video_000018 chunk_0008 failure: a 900s excerpt whose
    # model timeline ran to 1499s. The old path clamped "Lab" onto skiing
    # footage at the chunk tail and silently deleted "Ski".
    _write_single_chunk_project(
        tmp_path,
        ",".join(
            [
                '{"title":"Drive","start_s":0,"end_s":431,"summary":"drive","confidence":0.8}',
                '{"title":"Campus","start_s":431,"end_s":848,"summary":"campus","confidence":0.8}',
                '{"title":"Lab","start_s":848,"end_s":1199,"summary":"lab","confidence":0.8}',
                '{"title":"Ski","start_s":1199,"end_s":1499,"summary":"ski","confidence":0.8}',
            ]
        ),
    )

    result = import_gemini_analysis(tmp_path)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")

    assert result["chunks_rescaled"] == 1
    assert result["events_resurrected"] == 1
    assert result["skipped"] == 0
    assert [event["title"] for event in events] == ["Drive", "Campus", "Lab", "Ski"]
    scale = 900 / 1499
    lab = events[2]
    assert abs(lab["start_s"] - (6195 + 848 * scale)) < 1
    assert abs(lab["end_s"] - (6195 + 1199 * scale)) < 1
    ski = events[3]
    assert abs(ski["end_s"] - 7095) < 1
    for event in events:
        assert "chunk_timeline_rescaled" in event["metadata"]["validation_notes"]
        assert event["review_status"] == "needs_review"


def test_minutes_as_seconds_chunk_timeline_multiplied(tmp_path):
    # video_000018 chunk_0002 mode: events at 10-15 "seconds" for content
    # spread over a 900s excerpt are minutes.
    _write_single_chunk_project(
        tmp_path,
        ",".join(
            [
                '{"title":"Halloween","start_s":10,"end_s":12,"summary":"a","confidence":0.8}',
                '{"title":"Cartoon","start_s":12,"end_s":13,"summary":"b","confidence":0.8}',
                '{"title":"Birthday","start_s":13,"end_s":15,"summary":"c","confidence":0.8}',
            ]
        ),
    )

    result = import_gemini_analysis(tmp_path)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")

    assert result["chunks_minutes_rescaled"] == 1
    assert result["events_dropped_sliver"] == 0
    assert [(event["title"], event["start_s"], event["end_s"]) for event in events] == [
        ("Halloween", 6795, 6915),
        ("Cartoon", 6915, 6975),
        ("Birthday", 6975, 7095),
    ]
    for event in events:
        assert "chunk_timeline_minutes_rescaled" in event["metadata"]["validation_notes"]


def test_minutes_mode_with_full_span_scenes_marks_events_unreliable(tmp_path):
    # Events crammed into the first tenth while scenes span the excerpt:
    # multiplying by 60 would blow past the excerpt, so units are
    # untrustworthy and the events are excluded rather than guessed at.
    _write_single_chunk_project(
        tmp_path,
        ",".join(
            [
                '{"title":"A","start_s":10,"end_s":12,"summary":"a","confidence":0.8}',
                '{"title":"B","start_s":12,"end_s":13,"summary":"b","confidence":0.8}',
                '{"title":"C","start_s":13,"end_s":15,"summary":"c","confidence":0.8}',
            ]
        ),
        extra_fields=',"scene_candidates":[{"start_s":0,"end_s":880}]',
    )

    result = import_gemini_analysis(tmp_path)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")

    assert events == []
    assert result["events_dropped_unreliable"] == 3
    assert result["chunks_minutes_rescaled"] == 0
    assert result["chunks_rescaled"] == 0
    assert result["skipped"] == 3


def test_sliver_events_dropped_but_untimed_events_kept(tmp_path):
    _write_single_chunk_project(
        tmp_path,
        ",".join(
            [
                '{"title":"Sliver","start_s":100,"end_s":102,"summary":"blip","confidence":0.8}',
                '{"title":"Real","start_s":100,"end_s":200,"summary":"real","confidence":0.8}',
                '{"title":"Untimed","summary":"no clock","confidence":0.5}',
            ]
        ),
    )

    result = import_gemini_analysis(tmp_path)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")

    assert result["events_dropped_sliver"] == 1
    assert [event["title"] for event in events] == ["Real", "Untimed"]
    untimed = events[1]
    assert untimed["start_s"] == untimed["end_s"] == 6195
    assert "missing_time_range" in untimed["metadata"]["validation_notes"]


def test_import_all_gemini_analysis_runs_keeps_source_videos(tmp_path):
    (tmp_path / "tapes.jsonl").write_text(
        "\n".join(
            [
                '{"id":"video_000001","probe":{"duration_s":1000}}',
                '{"id":"video_000002","probe":{"duration_s":1000}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gemini_analyses.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"analysis_run_id":"run_a","source_video_id":"video_000001",'
                    '"chunk_id":"chunk_0001","chunk_index":1,"chunk_start_s":0,'
                    '"chunk_end_s":100,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"event_candidates":[{"title":"First tape event","start_s":10,'
                    '"end_s":20,"summary":"first","confidence":0.8}]}}'
                ),
                (
                    '{"analysis_run_id":"run_b","source_video_id":"video_000002",'
                    '"chunk_id":"chunk_0001","chunk_index":1,"chunk_start_s":0,'
                    '"chunk_end_s":100,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"event_candidates":[{"title":"Second tape event","start_s":30,'
                    '"end_s":40,"summary":"second","confidence":0.8}]}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = import_gemini_analysis(tmp_path, all_runs=True)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")
    evidence = read_jsonl(tmp_path / "gemini_evidence.jsonl")

    assert result["analysis_run_id"] == "all"
    assert result["selection"] == "best_per_source"
    assert result["source_video_ids"] == ["video_000001", "video_000002"]
    assert [(event["title"], event["source_video_id"]) for event in events] == [
        ("First tape event", "video_000001"),
        ("Second tape event", "video_000002"),
    ]
    assert [row["source_video_id"] for row in evidence] == ["video_000001", "video_000002"]


def test_import_all_prefers_whole_tape_over_older_chunks(tmp_path):
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_000001","probe":{"duration_s":1000}}\n',
        encoding="utf-8",
    )
    (tmp_path / "gemini_analyses.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"analysis_run_id":"chunk_run","source_video_id":"video_000001",'
                    '"chunk_id":"chunk_0001","chunk_index":1,"chunk_start_s":0,'
                    '"chunk_end_s":100,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"event_candidates":[{"title":"Chunk event","start_s":10,'
                    '"end_s":20,"summary":"chunk","confidence":0.8}]}}'
                ),
                (
                    '{"analysis_run_id":"whole_run","source_video_id":"video_000001",'
                    '"time_basis":"source_video",'
                    '"analysis":{"event_candidates":[{"title":"Whole tape event","start_s":30,'
                    '"end_s":40,"summary":"whole","confidence":0.9}]}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = import_gemini_analysis(tmp_path, all_runs=True)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")

    assert result["analyses"] == 1
    assert result["selection"] == "best_per_source"
    assert [(event["title"], event["start_s"], event["end_s"]) for event in events] == [
        ("Whole tape event", 30, 40),
    ]


def test_import_all_falls_back_to_chunks_when_whole_tape_timing_is_compressed(tmp_path):
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_000001","probe":{"duration_s":3600}}\n',
        encoding="utf-8",
    )
    (tmp_path / "gemini_analyses.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"analysis_run_id":"chunk_run","source_video_id":"video_000001",'
                    '"chunk_id":"chunk_0001","chunk_index":1,"chunk_start_s":1200,'
                    '"chunk_end_s":1300,"chunk_duration_s":100,"time_basis":"chunk",'
                    '"analysis":{"event_candidates":[{"title":"Aligned chunk event","start_s":10,'
                    '"end_s":20,"summary":"chunk","confidence":0.8}]}}'
                ),
                (
                    '{"analysis_run_id":"whole_run","source_video_id":"video_000001",'
                    '"time_basis":"source_video",'
                    '"analysis":{"event_candidates":['
                    '{"title":"Compressed A","start_s":10,"end_s":20},'
                    '{"title":"Compressed B","start_s":30,"end_s":40},'
                    '{"title":"Compressed C","start_s":50,"end_s":60}],'
                    '"scene_candidates":[{"start_s":0,"end_s":60}]}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = import_gemini_analysis(tmp_path, all_runs=True)
    events = read_jsonl(tmp_path / "gemini_events.jsonl")

    assert result["analyses"] == 1
    assert [(event["title"], event["start_s"], event["end_s"]) for event in events] == [
        ("Aligned chunk event", 1210, 1220),
    ]

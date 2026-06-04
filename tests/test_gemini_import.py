from tapesplit.gemini_import import import_gemini_analysis, _normalize_interval
from tapesplit.storage import read_jsonl


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

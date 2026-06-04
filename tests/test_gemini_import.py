from tapesplit.gemini_import import _normalize_interval


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

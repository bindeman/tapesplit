from tapesplit.windows import make_windows


def test_make_windows_covers_duration():
    windows = make_windows("video_000001", duration_s=65.0, window_seconds=30.0)

    assert len(windows) == 3
    assert windows[0]["start_s"] == 0
    assert windows[0]["end_s"] == 30
    assert windows[-1]["start_s"] == 60
    assert windows[-1]["end_s"] == 65


def test_make_windows_handles_empty_duration():
    assert make_windows("video_000001", duration_s=0.0, window_seconds=30.0) == []


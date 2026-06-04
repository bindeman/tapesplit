from tapesplit.non_content import FrameStats, classify_frame


def test_classifies_blue_screen():
    stats = FrameStats(
        time_s=0,
        mean_r=20,
        mean_g=60,
        mean_b=180,
        std_r=5,
        std_g=5,
        std_b=5,
    )

    assert classify_frame(stats) == "blue_screen_no_signal"


def test_classifies_black_screen():
    stats = FrameStats(
        time_s=0,
        mean_r=3,
        mean_g=3,
        mean_b=3,
        std_r=2,
        std_g=2,
        std_b=2,
    )

    assert classify_frame(stats) == "blank_black"


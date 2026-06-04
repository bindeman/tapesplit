from tapesplit.media_metadata import extract_date_candidates, parse_exif_date


def test_parse_exif_date_with_timezone():
    assert parse_exif_date("2026:03:16 08:46:06-07:00") == "2026-03-16T08:46:06-07:00"


def test_extract_date_candidates_marks_container_date():
    candidates = extract_date_candidates({"CreateDate": "2026:03:16 14:10:18"})

    assert candidates[0]["iso"] == "2026-03-16T14:10:18"
    assert candidates[0]["confidence"] == 0.65

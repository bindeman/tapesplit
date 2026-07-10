"""v2 M2 date model: origins, capture windows, and review routing."""

import json
from pathlib import Path

import pytest

from tapesplit.claim_store import ClaimStore, diff_v1_artifact
from tapesplit.date_model import (
    OriginContractError,
    build_capture_windows,
    classify_date_group,
    overlay_datestamps,
    validate_origin,
    window_check,
)
from tapesplit.grouping import build_project_groups
from tapesplit.storage import read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8"
    )


def test_overlay_datestamps_parse_camcorder_formats(tmp_path: Path):
    _write_jsonl(
        tmp_path / "visual_text_observations.jsonl",
        [
            {"source_video_id": "video_000001", "text": "SEP. 9 1998", "start_s": 10.0, "end_s": 12.0},
            {"source_video_id": "video_000001", "text": "APR 27 2002", "start_s": 40.0, "end_s": 42.0},
            {"source_video_id": "video_000001", "text": "1998", "start_s": 60.0, "end_s": 61.0},
            {"source_video_id": "video_000001", "text": "12005", "start_s": 70.0, "end_s": 71.0},
            {"source_video_id": "video_000001", "text": "no dates here", "start_s": 80.0, "end_s": 81.0},
        ],
    )
    stamps = overlay_datestamps(tmp_path)
    values = [(entry["date_value"], entry["precision"]) for entry in stamps["video_000001"]]
    assert ("1998-09-09", "day") in values
    assert ("2002-04-27", "day") in values
    assert ("1998", "year") in values
    assert len(values) == 3  # "12005" and prose must not parse


def test_capture_window_from_overlays_excludes_scan_exif_and_outlier_narration(tmp_path: Path):
    _write_jsonl(
        tmp_path / "visual_text_observations.jsonl",
        [
            {"source_video_id": "video_000001", "text": "MAY 25 2001", "start_s": 5.0, "end_s": 6.0},
            {"source_video_id": "video_000001", "text": "APR 27 2002", "start_s": 9.0, "end_s": 10.0},
        ],
    )
    _write_jsonl(
        tmp_path / "media_metadata.jsonl",
        [
            {
                "source_video_id": "video_000001",
                "date_candidates": [
                    {"field": "CreateDate", "iso": "2026-03-16T13:46:03", "confidence": 0.65},
                    {"field": "TrackCreateDate", "iso": "2002-05-01T00:00:00", "confidence": 0.55},
                ],
            }
        ],
    )
    windows = build_capture_windows(
        tmp_path,
        # A narrated 1912 full date must not widen the window.
        narrated_day_years={"video_000001": [2002, 1912]},
        media_ids=["video_000001", "video_000002"],
    )
    by_media = {row["media_id"]: row for row in windows}

    window = by_media["video_000001"]
    assert window["start_year"] == 2000 and window["end_year"] == 2003
    scan_rows = [obs for obs in window["observations"] if obs["basis"] == "exif_scan"]
    assert scan_rows and scan_rows[0]["year"] == 2026 and scan_rows[0]["excluded"]
    narrated_excluded = [
        obs for obs in window["observations"] if obs["basis"] == "narrated_day_date" and obs.get("excluded")
    ]
    assert [obs["year"] for obs in narrated_excluded] == [1912]

    # Media with no evidence of its own inherits the archive envelope.
    fallback = by_media["video_000002"]
    assert fallback["basis"] == ["archive_envelope"]
    assert fallback["start_year"] == 2000 and fallback["end_year"] == 2003
    assert fallback["confidence"] < window["confidence"]


def _windows(start: int, end: int) -> dict[str, dict]:
    return {"video_000001": {"media_id": "video_000001", "start_year": start, "end_year": end}}


def test_classification_day_precision_against_window():
    windows = _windows(2004, 2007)
    overlay = {"video_000001": [{"date_value": "2006-03-22", "year": 2006, "precision": "day"}]}

    corroborated = classify_date_group(
        {"date_value": "2006-03-22", "precision": "day"},
        media_ids=["video_000001"],
        windows_by_media=windows,
        overlays_by_media=overlay,
    )
    assert corroborated["origin"] == "overlay_datestamp"
    assert corroborated["excluded_as_event_date"] is False
    assert corroborated["capture_window_check"] == "inside"

    narrated = classify_date_group(
        {"date_value": "2005-06-01", "precision": "day"},
        media_ids=["video_000001"],
        windows_by_media=windows,
        overlays_by_media={},
    )
    assert narrated["origin"] == "narrated_current"
    assert narrated["excluded_as_event_date"] is False

    # The new catch: a full historical date the extractor trusted.
    historical = classify_date_group(
        {"date_value": "1912-06-06", "precision": "day"},
        media_ids=["video_000001"],
        windows_by_media=windows,
        overlays_by_media={},
    )
    assert historical["origin"] == "narrated_historical"
    assert historical["excluded_as_event_date"] is True
    assert historical["capture_window_check"] == "outside"


def test_classification_year_and_decade_routing():
    windows = _windows(2004, 2007)

    outside = classify_date_group(
        {"date_value": "1912", "precision": "year"},
        media_ids=["video_000001"],
        windows_by_media=windows,
        overlays_by_media={},
    )
    assert outside["origin"] == "narrated_historical"
    assert outside["excluded_as_event_date"] is True
    assert outside["needs_review"] is False

    inside = classify_date_group(
        {"date_value": "2005", "precision": "year"},
        media_ids=["video_000001"],
        windows_by_media=windows,
        overlays_by_media={},
    )
    assert inside["origin"] == "narrated_current"
    assert inside["excluded_as_event_date"] is True  # never a chronology bucket on its own
    assert inside["needs_review"] is True  # resolve_date routing

    no_window = classify_date_group(
        {"date_value": "2005", "precision": "year"},
        media_ids=["video_000009"],
        windows_by_media=windows,
        overlays_by_media={},
    )
    assert no_window["origin"] is None
    assert no_window["needs_review"] is True

    decade = classify_date_group(
        {"date_value": "1990s", "precision": "decade"},
        media_ids=["video_000001"],
        windows_by_media=windows,
        overlays_by_media={},
    )
    assert decade["origin"] == "narrated_historical"


def test_origin_contract_rejects_unreviewed_none_and_unknown_values():
    validate_origin(None, needs_review=True)
    validate_origin("overlay_datestamp", needs_review=False)
    with pytest.raises(OriginContractError):
        validate_origin(None, needs_review=False)
    with pytest.raises(OriginContractError):
        validate_origin("vibes", needs_review=False)


def test_window_check_no_media_coverage():
    assert window_check(2005, ["video_000404"], _windows(2004, 2007)) == "no_window"
    assert window_check(None, ["video_000001"], _windows(2004, 2007)) == "no_window"


def test_build_groups_emits_origins_windows_and_oracle_clean_claims(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TAPESPLIT_CLAIMS", "1")
    _write_jsonl(
        tmp_path / "tapes.jsonl",
        [{"id": "video_000001", "filename": "tape1.mp4", "probe": {"duration_s": 3600.0}}],
    )
    _write_jsonl(
        tmp_path / "visual_text_observations.jsonl",
        [{"source_video_id": "video_000001", "text": "MAR 22 2006", "start_s": 100.0, "end_s": 102.0}],
    )
    _write_jsonl(
        tmp_path / "evidence.jsonl",
        [{"id": "ev_1", "source_video_id": "video_000001"}],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Volcano Walk",
                "start_s": 90.0,
                "end_s": 400.0,
                "confidence": 0.9,
                "evidence_ids": ["ev_1"],
                "metadata": {
                    "date_candidates": ["MAR 22 2006", "1912 (eruption)"],
                    "people": ["Filip"],
                    "relatedness": "family",
                },
            }
        ],
    )

    summary = build_project_groups(tmp_path)
    assert summary["capture_windows"] == 1

    groups = {row["date_value"]: row for row in read_jsonl(tmp_path / "date_groups.jsonl")}
    assert groups["2006-03-22"]["origin"] == "overlay_datestamp"
    assert groups["2006-03-22"]["excluded_as_event_date"] is False
    assert groups["2006-03-22"]["capture_window_check"] == "inside"
    assert groups["1912"]["origin"] == "narrated_historical"
    assert groups["1912"]["excluded_as_event_date"] is True
    assert groups["1912"]["capture_window_check"] == "outside"

    window = read_jsonl(tmp_path / "capture_windows.jsonl")[0]
    assert window["media_id"] == "video_000001"
    assert window["start_year"] == 2005 and window["end_year"] == 2007

    for artifact in ("date_groups.jsonl", "capture_windows.jsonl"):
        verdict = diff_v1_artifact(tmp_path, artifact)
        assert verdict["byte_identical"], verdict

    with ClaimStore(tmp_path) as store:
        kinds = {claim["kind"] for claim in store.claims(v1_artifact="date_groups.jsonl")}
    assert kinds == {"date"}

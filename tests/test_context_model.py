"""v2 context model (M3): typed contexts, break signals, contradiction guard."""

import json
from pathlib import Path

from tapesplit.context_model import (
    CONTRADICTION_CONFIDENCE_CAP,
    build_era_contexts,
    build_segment_contexts,
    classify_context_candidate,
    era_for_year,
    load_anchor_lookup,
    parse_region,
    region_label,
    regions_disjoint,
    supersede_machine_place_context_corrections,
)
from tapesplit.review_actions import _place_context_block_reason, reapply_review_corrections


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_parse_region_variants():
    assert parse_region("Eugene, Oregon, United States") == {"city": "eugene", "state": "oregon", "country": "us"}
    assert parse_region("Moscow, Russia") == {"city": "moscow", "state": None, "country": "ru"}
    assert parse_region("Ryazan Oblast, Russia") == {"city": None, "state": "ryazan", "country": "ru"}
    assert parse_region("Hawaii") == {"city": None, "state": "hawaii", "country": "us"}


def test_regions_disjoint_rules():
    eugene = parse_region("Eugene, Oregon, United States")
    oregon = parse_region("Oregon, United States")
    moscow_ru = parse_region("Moscow, Russia")
    moscow_id = parse_region("Moscow, Idaho, United States")
    assert regions_disjoint(moscow_ru, moscow_id)  # different countries
    assert regions_disjoint(eugene, moscow_id)  # different states
    assert not regions_disjoint(eugene, oregon)  # containment, not conflict
    assert not regions_disjoint(eugene, eugene)


def test_contradiction_guard_caps_and_flags():
    verdict = classify_context_candidate(
        basis="continuity_admin_context",
        group_anchor={"region": parse_region("Moscow, Russia"), "basis": "verification_vote"},
        parent_anchor={"region": parse_region("Eugene, Oregon, United States"), "basis": "geocode"},
        confidence=0.9,
    )
    assert verdict["contradiction"] is True
    assert verdict["confidence"] == CONTRADICTION_CONFIDENCE_CAP
    assert verdict["context_kind"] == "era"
    assert "disjoint" in verdict["notes"][0]

    clean = classify_context_candidate(
        basis="direct_admin_context",
        group_anchor={"region": parse_region("Eugene, Oregon, United States"), "basis": "geocode"},
        parent_anchor={"region": parse_region("Oregon, United States"), "basis": "geocode"},
        confidence=0.74,
    )
    assert clean["contradiction"] is False
    assert clean["context_kind"] == "geo"
    assert clean["confidence"] == 0.74


def test_safe_policy_blocks_contradicted_context():
    row = {"metadata": {"contradiction": True, "contradiction_notes": ["Anchored to Moscow, Russia"]}}
    reason = _place_context_block_reason(row)
    assert reason and "contradicted" in reason
    assert _place_context_block_reason({"metadata": {}}) is None


def test_anchor_lookup_prefers_votes_over_geocodes(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write(
        project / "place_geocodes.jsonl",
        [
            {
                "place_label": "Maplewood School",
                "confidence": 0.8,
                "context_suspect": False,
                "selected": {"city": "Hialeah", "state": "Florida", "country": "United States", "lat": 25.8, "lng": -80.3},
            }
        ],
    )
    _write(
        project / "verification_votes.jsonl",
        [
            {
                "kind": "place_context_geo",
                "place": "Maplewood School",
                "verdict": "support",
                "actual_region": "Eugene, Oregon, United States",
                "confidence": 0.95,
            }
        ],
    )
    lookup = load_anchor_lookup(project)
    anchor = lookup["maplewood school"]
    assert anchor["basis"] == "verification_vote"
    assert region_label(anchor["region"]) == "Eugene, Oregon"


def test_unparseable_refutation_becomes_veto_anchor(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write(
        project / "place_geocodes.jsonl",
        [
            {
                "place_label": "Brazil",
                "confidence": 0.7,
                "context_suspect": False,
                "selected": {"city": "Brazil", "state": "Oregon", "country": "United States", "lat": 44.0, "lng": -123.0},
            }
        ],
    )
    _write(
        project / "verification_votes.jsonl",
        [
            {
                "kind": "place_context_geo",
                "place": "Brazil",
                "verdict": "refute",
                "actual_region": "South America",
                "confidence": 0.97,
            }
        ],
    )
    from tapesplit.context_model import anchor_asserts_region

    anchor = load_anchor_lookup(project)["brazil"]
    assert anchor.get("veto") is True, "refutation must outrank the bad geocode"
    assert not anchor_asserts_region(anchor), "a veto anchor asserts no geography"


def test_segments_break_on_gap_region_and_language(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write(project / "non_content_ranges.jsonl", [{"source_video_id": "video_1", "start_s": 100.0, "end_s": 300.0}])
    events = [
        {"id": "e1", "start_s": 0.0, "end_s": 90.0},
        {"id": "e2", "start_s": 310.0, "end_s": 400.0},  # after the non-content gap
        {"id": "e3", "start_s": 410.0, "end_s": 500.0},  # disjoint anchored region vs e2
        {"id": "e4", "start_s": 510.0, "end_s": 600.0},  # language shift vs e3
    ]
    segments, assignment = build_segment_contexts(
        project,
        events,
        event_media={eid: ["video_1"] for eid in ("e1", "e2", "e3", "e4")},
        event_years={},
        event_regions={
            "e2": parse_region("Moscow, Russia"),
            "e3": parse_region("Eugene, Oregon, United States"),
        },
        event_languages={"e3": "ru", "e4": "en"},
    )
    assert assignment["e1"] != assignment["e2"], "non-content gap must break the segment"
    assert assignment["e2"] != assignment["e3"], "anchored region change must break the segment"
    assert assignment["e3"] != assignment["e4"], "language shift must break the segment"
    reasons = [segment["break_after"] for segment in segments]
    assert "non_content_gap" in reasons and "anchored_region_change" in reasons


def test_era_inference_and_move_year():
    moscow = parse_region("Moscow, Idaho, United States")
    eugene = parse_region("Eugene, Oregon, United States")
    votes = [
        (1999, moscow, "school_anchor"),
        (2000, moscow, "school_anchor"),
        (2001, moscow, "residence_role"),
        (2003, eugene, "school_anchor"),
        (2004, eugene, "school_anchor"),
        # A conflicting weak vote in 2003 must lose to the school anchor.
        (2003, moscow, "residence_role"),
    ]
    eras = build_era_contexts(residence_votes=votes)
    assert [era["residence_label"] for era in eras] == ["Moscow, Idaho", "Eugene, Oregon"]
    assert eras[0]["end_year"] == 2001 and eras[1]["start_year"] == 2003
    assert 2003 in eras[1]["conflict_years"]
    assert era_for_year(eras, 2000)["residence_label"] == "Moscow, Idaho"
    assert era_for_year(eras, 2004)["residence_label"] == "Eugene, Oregon"


def test_remediation_supersedes_only_machine_context_corrections(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write(
        project / "corrections.jsonl",
        [
            {"action": "confirm_place_context", "target_id": "ce_1", "reviewer": "auto-pipeline"},
            {"action": "confirm_place_context", "target_id": "ce_2", "reviewer": "phillip"},
            {"action": "confirm_person", "target_id": "pg_1", "reviewer": "auto-pipeline"},
        ],
    )
    result = supersede_machine_place_context_corrections(project)
    assert result["superseded"] == 1
    rows = [json.loads(line) for line in (project / "corrections.jsonl").read_text().splitlines()]
    assert rows[0]["superseded"] is True and rows[0]["superseded_by"] == "m3_context_v2"
    assert "superseded" not in rows[1] and "superseded" not in rows[2]

    replay = reapply_review_corrections(project)
    assert replay["corrections_superseded"] == 1

"""Second-opinion event re-assembly: adjustments, promotions, corrections remap."""

import json
from pathlib import Path

from tapesplit.event_reassembly import (
    build_event_reassembly,
    remap_event_corrections,
)
from tapesplit.event_stitching import load_source_events
from tapesplit.storage import append_jsonl, read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)


def _project_with_adjudication(tmp_path: Path, *, verifications: list[dict] | None = None) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    _write_jsonl(
        project / "gemini_events.jsonl",
        [
            {
                "id": "gem_event_000001",
                "title": "NBC Sports Schedule",
                "start_s": 511.0,
                "end_s": 540.0,
                "confidence": 0.8,
                "source": "gemini",
                "metadata": {"source_video_id": "video_000001", "time_basis": "source"},
            },
            {
                "id": "gem_event_000002",
                "title": "Philip sleeping",
                "start_s": 18.0,
                "end_s": 105.0,
                "confidence": 0.7,
                "source": "gemini",
                "metadata": {"source_video_id": "video_000009", "time_basis": "source"},
            },
            {
                "id": "gem_event_000003",
                "title": "Garden afternoon",
                "start_s": 900.0,
                "end_s": 1000.0,
                "confidence": 0.8,
                "source": "gemini",
                "metadata": {"source_video_id": "video_000002", "time_basis": "source"},
            },
        ],
    )
    _write_jsonl(
        project / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "NBC Sports Schedule",
                "metadata": {
                    "source_event_ids": ["gem_event_000001"],
                    "source_event_count": 1,
                    "source_ranges": [
                        {"source_video_id": "video_000001", "start_s": 511.0, "end_s": 540.0}
                    ],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "Philip sleeping",
                "metadata": {
                    "source_event_ids": ["gem_event_000002"],
                    "source_event_count": 1,
                    "source_ranges": [
                        {"source_video_id": "video_000009", "start_s": 18.0, "end_s": 105.0}
                    ],
                },
            },
            {
                "id": "canonical_event_000003",
                "title": "Garden afternoon",
                "metadata": {
                    "source_event_ids": ["gem_event_000003"],
                    "source_event_count": 1,
                    "source_ranges": [
                        {"source_video_id": "video_000002", "start_s": 900.0, "end_s": 1000.0}
                    ],
                },
            },
        ],
    )
    (project / "azure_adjudication.json").write_text(
        json.dumps(
            {
                "created_at": "2026-07-10T09:25:00+00:00",
                "counts": {},
                "verdicts": {
                    "range_disputed": [
                        {
                            "canonical_event_id": "canonical_event_000001",
                            "source_video_id": "video_000001",
                            "start_s": 511.0,
                            "end_s": 540.0,
                            "azure_title": "Chrysler commercial",
                            "azure_range": [1391.0, 1422.0],
                        }
                    ],
                    "content_disputed": [
                        {
                            "canonical_event_id": "canonical_event_000002",
                            "source_video_id": "video_000009",
                            "start_s": 18.0,
                            "end_s": 105.0,
                            "azure_title": "Children before school",
                        }
                    ],
                    "corroborated": [],
                    "uncovered": [],
                },
                "azure_only": [
                    {
                        "title": "First day at elementary school",
                        "source_video_id": "video_000009",
                        "start_s": 500.0,
                        "end_s": 700.0,
                        "confidence": 0.96,
                        "relatedness": "likely_family",
                    },
                    {
                        "title": "Backyard sprinkler run",
                        "source_video_id": "video_000009",
                        "start_s": 800.0,
                        "end_s": 860.0,
                        "confidence": 0.75,
                        "relatedness": "likely_family",
                    },
                    {
                        "title": "Low confidence blur",
                        "source_video_id": "video_000009",
                        "start_s": 900.0,
                        "end_s": 950.0,
                        "confidence": 0.6,
                        "relatedness": "likely_family",
                    },
                    {
                        "title": "Duplicate of known event",
                        "source_video_id": "video_000001",
                        "start_s": 515.0,
                        "end_s": 535.0,
                        "confidence": 0.95,
                        "relatedness": "likely_family",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    if verifications:
        _write_jsonl(project / "verifications.jsonl", verifications)
    return project


def test_reassembly_applies_disputes_and_promotions(tmp_path: Path):
    project = _project_with_adjudication(tmp_path)

    summary = build_event_reassembly(project)

    assert summary["range_corrections"] == 1
    assert summary["content_disputes_flagged"] == 1
    assert summary["promoted"] == 2
    assert summary["promoted_needs_review"] == 1
    assert summary["promotion_backlog"] == 1
    assert summary["deduped_against_canonical"] == 1

    adjustments = read_jsonl(project / "source_event_adjustments.jsonl")
    range_fix = next(row for row in adjustments if row["basis"] == "azure_range_dispute")
    assert range_fix["new"] == {"start_s": 1391.0, "end_s": 1422.0}

    azure_events = read_jsonl(project / "azure_events.jsonl")
    assert {row["title"] for row in azure_events} == {
        "First day at elementary school",
        "Backyard sprinkler run",
    }
    review_flags = {row["title"]: row["review_status"] for row in azure_events}
    assert review_flags["Backyard sprinkler run"] == "needs_review"
    assert review_flags["First day at elementary school"] == "unreviewed"


def test_load_source_events_folds_in_reassembly(tmp_path: Path):
    project = _project_with_adjudication(tmp_path)
    build_event_reassembly(project)

    events = {row["id"]: row for row in load_source_events(project)}

    corrected = events["gem_event_000001"]
    assert corrected["start_s"] == 1391.0 and corrected["end_s"] == 1422.0
    assert any(n.startswith("range_corrected") for n in corrected["metadata"]["validation_notes"])

    disputed = events["gem_event_000002"]
    assert disputed["review_status"] == "needs_review"
    assert disputed["metadata"]["second_opinion_title"] == "Children before school"
    # original range untouched on a content dispute
    assert disputed["start_s"] == 18.0

    assert "az_event_000002" in events or "az_event_000001" in events
    promoted = [row for row in events.values() if row.get("source") == "azure_second_opinion"]
    assert len(promoted) == 2


def test_regrounding_gated_by_margin_and_measured_precision(tmp_path: Path):
    weak_tape_verifications = [
        {
            "claim_type": "event_content",
            "verdict": "CONTRADICTED",
            "source_video_id": "video_000002",
        }
        for _ in range(8)
    ]
    project = _project_with_adjudication(tmp_path, verifications=weak_tape_verifications)
    _write_jsonl(
        project / "event_regroundings.jsonl",
        [
            {
                "canonical_event_id": "canonical_event_000003",
                "source_video_id": "video_000002",
                "current_start_s": 900.0,
                "current_end_s": 1000.0,
                "proposed_start_s": 1200.0,
                "proposed_end_s": 1300.0,
                "margin": 0.1,
                "confidence": 0.7,
            },
            {
                "canonical_event_id": "canonical_event_000002",
                "source_video_id": "video_000009",
                "current_start_s": 18.0,
                "current_end_s": 105.0,
                "proposed_start_s": 200.0,
                "proposed_end_s": 300.0,
                "margin": 0.1,
                "confidence": 0.7,
            },
        ],
    )

    summary = build_event_reassembly(project)

    # weak tape (measured 0% precision) accepts margin 0.1; healthy tape defers —
    # and canonical_event_000002 is already azure-disputed anyway.
    assert summary["measured_weak_tapes"] == ["video_000002"]
    assert summary["regrounding_applied"] == 1
    applied = [
        row for row in read_jsonl(project / "source_event_adjustments.jsonl") if row["basis"] == "regrounding"
    ]
    assert len(applied) == 1 and applied[0]["source_video_id"] == "video_000002"


def test_reassembly_skips_without_adjudication(tmp_path: Path):
    project = tmp_path / "empty.tapesplit"
    project.mkdir()
    summary = build_event_reassembly(project)
    assert summary["skipped"] == "no azure_adjudication.json"
    assert load_source_events(project) == []


def test_remap_event_corrections_rewrites_targets(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    before = [
        {
            "id": "canonical_event_000210",
            "title": "Birthday Party",
            "metadata": {
                "source_ranges": [
                    {"source_video_id": "video_000005", "start_s": 100.0, "end_s": 200.0}
                ]
            },
        }
    ]
    after = [
        {
            "id": "canonical_event_000305",
            "title": "Birthday Party at Home",
            "metadata": {
                "source_ranges": [
                    {"source_video_id": "video_000005", "start_s": 95.0, "end_s": 210.0}
                ]
            },
        }
    ]
    _write_jsonl(
        project / "corrections.jsonl",
        [
            {
                "id": "correction_000016",
                "action": "confirm_event",
                "target_id": "canonical_event_000210",
                "target_type": "canonical_event",
                "payload": {"title": "Birthday Party"},
            },
            {
                "id": "correction_000001",
                "action": "confirm_person",
                "target_id": "people_group_000005",
                "target_type": "person_group",
            },
        ],
    )

    result = remap_event_corrections(project, before_events=before, after_events=after)

    assert result["remapped"] == 1
    assert result["unmatched"] == []
    rows = {row["id"]: row for row in read_jsonl(project / "corrections.jsonl")}
    assert rows["correction_000016"]["target_id"] == "canonical_event_000305"
    assert rows["correction_000016"]["metadata"]["remapped_from"] == "canonical_event_000210"
    assert rows["correction_000001"]["target_id"] == "people_group_000005"
    assert (project / "corrections.jsonl.pre_reassembly").exists()

"""Closed-loop calibration of the safe auto-accept policy.

Every review decision recorded in ``corrections.jsonl`` is ground truth about
how good the system's suggestions were:

- a human confirming a suggestion → the suggestion type was right
- a human rejecting a suggested candidate → the suggestion type was wrong
- an auto-accepted correction later rejected/edited by a human → the safe
  policy accepted something it should not have (the strongest signal)

``calibrate_review_policy`` turns those outcomes into per-action-type
precision estimates and tunes the safe-policy confidence floors, writing them
to ``review_policy.json`` in the project. ``apply_review_suggestions`` (and
therefore ``tapesplit auto`` and the UI's bulk accept) reads that file, so
each project's acceptance thresholds tighten or relax based on how its own
reviewers have corrected the machine — a per-archive learning loop that needs
no model retraining.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapesplit.storage import read_jsonl, write_json

REVIEW_POLICY_FILENAME = "review_policy.json"
REVIEW_POLICY_SCHEMA_VERSION = 1

# Reviewers whose corrections were produced by automation, not a human.
AUTO_REVIEWERS = {"auto-pipeline", "bulk-suggestion", "review-ui-bulk"}

# Rejection/edit actions mapped to the confirm action they refute.
REFUTES: dict[str, str] = {
    "reject_identity": "confirm_identity",
    "reject_speaker_identity": "confirm_speaker_identity",
    "reject_relationship": "confirm_relationship",
    "edit_relationship": "confirm_relationship",
    "reject_place_context": "confirm_place_context",
    "mark_not_location": "confirm_place",
    "rename_place": "confirm_place",
    "rename_person": "confirm_person",
    "rename_event": "confirm_event",
    "edit_date": "confirm_event_date",
}

CONFIRM_ACTIONS = {
    "confirm_identity",
    "confirm_speaker_identity",
    "confirm_person",
    "merge_person",
    "confirm_place",
    "confirm_place_context",
    "confirm_relationship",
    "confirm_event",
    "confirm_event_date",
}

# How precise each action type should be before auto-accepting freely, and
# the floor range calibration may move within.
TARGETS: dict[str, tuple[float, float, float]] = {
    # action: (target_precision, floor_min, floor_max)
    "confirm_identity": (0.95, 0.6, 0.95),
    "confirm_speaker_identity": (0.9, 0.55, 0.95),
    "confirm_person": (0.9, 0.5, 0.9),
    "merge_person": (0.95, 0.6, 0.95),
    "confirm_place": (0.9, 0.5, 0.9),
    "confirm_place_context": (0.85, 0.45, 0.9),
    "confirm_relationship": (0.95, 0.65, 0.95),
    "confirm_event": (0.85, 0.45, 0.85),
    "confirm_event_date": (0.9, 0.5, 0.9),
}

MIN_OBSERVATIONS = 8
RAISE_STEP_LIMIT = 0.15
LOWER_STEP_LIMIT = 0.05


def analyze_review_outcomes(project_dir: Path) -> dict[str, dict[str, int]]:
    """Per confirm-action outcome counts derived from the corrections log."""

    project = project_dir.expanduser().resolve()
    corrections = read_jsonl(project / "corrections.jsonl")

    outcomes: dict[str, dict[str, int]] = {}

    def bucket(action: str) -> dict[str, int]:
        return outcomes.setdefault(
            action,
            {"human_confirmed": 0, "human_rejected": 0, "auto_accepted": 0, "auto_overridden": 0},
        )

    auto_confirmed_targets: dict[str, str] = {}  # target_id -> confirm action

    for row in corrections:
        action = str(row.get("action") or "")
        target_id = str(row.get("target_id") or "")
        reviewer = str(row.get("reviewer") or "")
        is_auto = reviewer in AUTO_REVIEWERS

        if action in CONFIRM_ACTIONS:
            if is_auto:
                bucket(action)["auto_accepted"] += 1
                if target_id:
                    auto_confirmed_targets[target_id] = action
            else:
                bucket(action)["human_confirmed"] += 1
            continue

        refuted = REFUTES.get(action)
        if refuted is None:
            continue
        prior_auto = auto_confirmed_targets.pop(target_id, None) if target_id else None
        if prior_auto:
            # A human walked back something automation accepted.
            bucket(prior_auto)["auto_overridden"] += 1
        else:
            bucket(refuted)["human_rejected"] += 1

    return outcomes


def observed_precision(counts: dict[str, int]) -> tuple[float, int]:
    """Laplace-smoothed precision and the observation count it rests on.

    Auto-overrides are weighted double: automation confidently asserting
    something a human had to undo is worse than offering a candidate a human
    declines.
    """

    positive = counts.get("human_confirmed", 0)
    negative = counts.get("human_rejected", 0) + 2 * counts.get("auto_overridden", 0)
    observations = positive + counts.get("human_rejected", 0) + counts.get("auto_overridden", 0)
    precision = (positive + 1) / (positive + negative + 2)
    return round(precision, 4), observations


def calibrate_review_policy(project_dir: Path, *, write: bool = True) -> dict[str, Any]:
    """Tune per-action confidence floors from review outcomes."""

    from tapesplit.review_actions import SAFE_AUTO_ACCEPT_MIN_CONFIDENCE

    project = project_dir.expanduser().resolve()
    outcomes = analyze_review_outcomes(project)

    floors: dict[str, float] = {}
    report: dict[str, Any] = {}
    for action, (target, floor_min, floor_max) in TARGETS.items():
        base = SAFE_AUTO_ACCEPT_MIN_CONFIDENCE.get(action)
        if base is None:
            continue
        counts = outcomes.get(action, {})
        precision, observations = observed_precision(counts)
        floor = float(base)
        adjustment = "insufficient-data"
        if observations >= MIN_OBSERVATIONS:
            if precision < target:
                floor = min(floor_max, floor + min(RAISE_STEP_LIMIT, (target - precision) * 0.5))
                adjustment = "raised"
            else:
                floor = max(floor_min, floor - min(LOWER_STEP_LIMIT, (precision - target) * 0.25))
                adjustment = "lowered" if floor < float(base) else "kept"
        floors[action] = round(floor, 3)
        report[action] = {
            "base_floor": base,
            "tuned_floor": floors[action],
            "observed_precision": precision,
            "observations": observations,
            "adjustment": adjustment,
            "counts": counts,
        }

    policy = {
        "schema_version": REVIEW_POLICY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corrections_analyzed": len(read_jsonl(project / "corrections.jsonl")),
        "min_confidence_by_action": floors,
        "report": report,
    }
    if write:
        write_json(project / REVIEW_POLICY_FILENAME, policy)
    return policy


def load_review_policy_floors(project_dir: Path) -> dict[str, float]:
    """Tuned floors for this project, or empty when never calibrated."""

    path = project_dir.expanduser().resolve() / REVIEW_POLICY_FILENAME
    if not path.exists():
        return {}
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        floors = policy.get("min_confidence_by_action")
        if not isinstance(floors, dict):
            return {}
        return {str(action): float(value) for action, value in floors.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}

"""Age dimension: per-person birth-year posteriors from face ages + dated footage.

No open face model bridges a child aging 2→12 (verification collapses to ~53%
at a 3-year gap), so cross-era identity needs evidence embeddings cannot give.
This module turns the age estimates that already ride along with embeddings
(buffalo_l genderage per observation, quality-weighted per track) into a
per-person birth-year posterior: every attributed face seen in *dated* footage
is a sample of (footage year − apparent age). Two consumers:

- identity_fusion's `age_trajectory` dimension: two person-groups whose
  samples agree on one birth year across different eras are likely the same
  aging person (the merge evidence ArcFace can't provide on children); a
  child and an adult sharing an era can never merge.
- `age_anchor` date claims (REFOUNDATION §5, confidence ≤ 0.6): for media
  with no better date signal, people with known birth years bound the capture
  year — the dating engine the scanned-photo track needs.

Ages on VHS are noisy; sigmas are deliberately wide (±2-3y children, ±4-6y
adults) and everything is claims + posterior inputs, never a hard fact.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from tapesplit.claim_store import DualWriter
from tapesplit.face_tracks import load_face_tracks
from tapesplit.storage import read_jsonl

PERSON_AGE_MODELS_FILENAME = "person_age_models.jsonl"

# Attribution: a cluster speaks for a person when a human linked it, or when
# its top identity candidate is at least this confident.
CLUSTER_ATTRIBUTION_MIN_CONFIDENCE = 0.55
HUMAN_LINK_CONFIDENCE = 0.95
# Age sigma by apparent-age band (children estimate tighter than adults).
CHILD_AGE_MAX = 14.0
CHILD_AGE_SIGMA = 2.5
ADULT_AGE_SIGMA = 5.0
ADULT_MEDIAN_AGE = 18.0
MIN_SAMPLES_FOR_MODEL = 2
AGE_ANCHOR_MAX_CONFIDENCE = 0.6


def build_person_age_models(project_dir: Path) -> dict[str, Any]:
    """Aggregate attributed face ages against footage years into birth-year models."""

    project = project_dir.expanduser().resolve()
    clusters = read_jsonl(project / "face_clusters.jsonl")
    candidates = read_jsonl(project / "face_identity_candidates.jsonl")
    observations = read_jsonl(project / "face_observations.jsonl")
    embeddings = {str(row.get("id") or ""): row for row in read_jsonl(project / "face_embeddings.jsonl")}
    tracks = load_face_tracks(project)

    attribution = _cluster_attribution(clusters, candidates)
    media_years = _media_year_estimates(project)

    samples_by_person: dict[str, list[dict[str, Any]]] = {}

    def add_sample(
        person_id: str,
        *,
        age: float,
        media_id: str,
        time_s: float | None,
        weight: float,
        basis: str,
        attribution_kind: str,
    ) -> None:
        year = _year_at(media_years.get(media_id), time_s)
        if year is None:
            return
        sigma = CHILD_AGE_SIGMA if age <= CHILD_AGE_MAX else ADULT_AGE_SIGMA
        samples_by_person.setdefault(person_id, []).append(
            {
                "age": round(age, 1),
                "year": year["year"],
                "year_sigma": year["sigma"],
                "birth_year": round(year["year"] - age, 1),
                "sigma": round(math.sqrt(sigma**2 + year["sigma"] ** 2), 2),
                "weight": round(max(0.01, weight), 4),
                "media_id": media_id,
                "basis": basis,
                "attribution": attribution_kind,
            }
        )

    # Track ages first (quality-weighted medians over many frames beat any
    # single crop); observations already inside a track are skipped below.
    tracked_observation_ids: set[str] = set()
    for track in tracks:
        tracked_observation_ids.update(str(m) for m in track.get("member_face_observation_ids") or [])
        cluster_id = str(track.get("face_cluster_id") or "")
        attributed = attribution.get(cluster_id)
        age = _number_or_none(track.get("age_estimate"))
        if not attributed or age is None:
            continue
        person_id, confidence, kind = attributed
        add_sample(
            person_id,
            age=age,
            media_id=str(track.get("media_id") or ""),
            time_s=_number_or_none((track.get("span") or {}).get("start_s")),
            weight=confidence * float((track.get("quality") or {}).get("mean_w") or 0.3),
            basis=f"track:{track.get('id')}",
            attribution_kind=kind,
        )

    for observation in observations:
        observation_id = str(observation.get("id") or "")
        if observation_id in tracked_observation_ids:
            continue
        cluster_id = str(observation.get("face_cluster_id") or "")
        attributed = attribution.get(cluster_id)
        if not attributed:
            continue
        age = _number_or_none((embeddings.get(observation_id) or {}).get("age_raw"))
        if age is None:
            continue
        person_id, confidence, kind = attributed
        add_sample(
            person_id,
            age=age,
            media_id=str(observation.get("source_video_id") or ""),
            time_s=_number_or_none(observation.get("time_s") or observation.get("start_s")),
            weight=confidence * float(observation.get("face_quality_weight") or 0.3),
            basis=f"observation:{observation_id}",
            attribution_kind=kind,
        )

    models = []
    for person_id, samples in sorted(samples_by_person.items()):
        if len(samples) < MIN_SAMPLES_FOR_MODEL:
            continue
        models.append(_person_model(person_id, samples))

    _write_models(project, models)
    claims = _write_claims(project, models, tracks, attribution, media_years)
    return {
        "people_modeled": len(models),
        "samples": sum(model["sample_count"] for model in models),
        "from_tracks": sum(
            1
            for model in models
            for sample in model["samples"]
            if str(sample.get("basis") or "").startswith("track:")
        ),
        "age_anchor_claims": claims.get("age_anchor_claims", 0),
        "attribute_claims": claims.get("attribute_claims", 0),
        "claims_error": claims.get("error"),
        "output": str(project / PERSON_AGE_MODELS_FILENAME),
    }


def load_person_age_models(project: Path) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("person_group_id") or ""): row
        for row in read_jsonl(project / PERSON_AGE_MODELS_FILENAME)
        if isinstance(row, dict)
    }


def _person_model(person_id: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    birth_years = [float(sample["birth_year"]) for sample in samples]
    weights = [float(sample["weight"]) for sample in samples]
    birth_year = _weighted_median(birth_years, weights)
    deviations = [abs(value - birth_year) for value in birth_years]
    mad = _weighted_median(deviations, weights)
    mean_sample_sigma = sum(float(sample["sigma"]) for sample in samples) / len(samples)
    # Spread and per-sample uncertainty both bound the posterior; a floor keeps
    # single-era models from claiming false precision.
    sigma = max(1.5, 1.4826 * mad, mean_sample_sigma / math.sqrt(len(samples)))
    ages = sorted(float(sample["age"]) for sample in samples)
    median_age = ages[len(ages) // 2]
    human_backed = any(sample.get("attribution") == "confirmed" for sample in samples)
    years = sorted({int(sample["year"]) for sample in samples})
    return {
        "person_group_id": person_id,
        "birth_year": round(birth_year, 1),
        "sigma": round(sigma, 2),
        "sample_count": len(samples),
        "median_apparent_age": round(median_age, 1),
        "adult": median_age >= ADULT_MEDIAN_AGE,
        "observed_years": years,
        "attribution_basis": "confirmed" if human_backed else "candidate",
        "samples": sorted(samples, key=lambda sample: sample["year"])[:40],
    }


# --- footage-year estimation -------------------------------------------------


def _media_year_estimates(project: Path) -> dict[str, dict[str, Any]]:
    """Per-media year estimates: dated-event intervals, else tape-level medians.

    Priority: (a) non-excluded date groups mapped through their events'
    source ranges (sigma 0.5 within the interval); (b) the tape-level median
    of non-excluded date-group years (sigma 1.0); (c) media_metadata date
    candidates (sigma 1.5). Narrated-historical dates never participate —
    excluded_as_event_date is honored, not re-litigated.
    """

    events = {
        str(event.get("id") or ""): event for event in read_jsonl(project / "canonical_events.jsonl")
    }
    intervals_by_media: dict[str, list[dict[str, Any]]] = {}
    years_by_media: dict[str, list[int]] = {}
    for group in read_jsonl(project / "date_groups.jsonl"):
        if group.get("excluded_as_event_date"):
            continue
        year = _year_from_value(group.get("date_value") or group.get("label"))
        if year is None:
            continue
        for event_id in group.get("canonical_event_ids") or []:
            event = events.get(str(event_id))
            if not event:
                continue
            for interval in _event_intervals(event):
                intervals_by_media.setdefault(interval["source_video_id"], []).append(
                    {**interval, "year": year}
                )
                years_by_media.setdefault(interval["source_video_id"], []).append(year)

    estimates: dict[str, dict[str, Any]] = {}
    for media_id, intervals in intervals_by_media.items():
        years = years_by_media.get(media_id) or []
        tape_year = sorted(years)[len(years) // 2] if years else None
        estimates[media_id] = {
            "intervals": intervals,
            "tape_year": tape_year,
            "tape_sigma": 1.0,
        }

    for row in read_jsonl(project / "media_metadata.jsonl"):
        media_id = str(row.get("source_video_id") or row.get("id") or "")
        if not media_id or media_id in estimates:
            continue
        for candidate in row.get("date_candidates") or []:
            year = _year_from_value(
                candidate.get("value") if isinstance(candidate, dict) else candidate
            )
            if year is not None:
                estimates[media_id] = {"intervals": [], "tape_year": year, "tape_sigma": 1.5}
                break
    return estimates


def _year_at(estimate: dict[str, Any] | None, time_s: float | None) -> dict[str, Any] | None:
    if not estimate:
        return None
    if time_s is not None:
        for interval in estimate.get("intervals") or []:
            if float(interval["start_s"]) <= time_s <= float(interval["end_s"]):
                return {"year": int(interval["year"]), "sigma": 0.5}
    if estimate.get("tape_year") is not None:
        return {"year": int(estimate["tape_year"]), "sigma": float(estimate.get("tape_sigma") or 1.0)}
    return None


def _year_from_value(value: Any) -> int | None:
    import re

    match = re.search(r"\b(19[2-9]\d|20[0-4]\d)\b", str(value or ""))
    return int(match.group(1)) if match else None


def _event_intervals(event: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    intervals = []
    for source in [event.get("source_ranges"), metadata.get("source_ranges")]:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            media_id = str(item.get("source_video_id") or "")
            start = _number_or_none(item.get("start_s"))
            end = _number_or_none(item.get("end_s"))
            if media_id and start is not None and end is not None:
                intervals.append({"source_video_id": media_id, "start_s": start, "end_s": end})
    media_id = str(event.get("source_video_id") or metadata.get("source_video_id") or "")
    start = _number_or_none(event.get("start_s"))
    end = _number_or_none(event.get("end_s"))
    if media_id and start is not None and end is not None:
        intervals.append({"source_video_id": media_id, "start_s": start, "end_s": end})
    return intervals


# --- cluster -> person attribution -------------------------------------------


def _cluster_attribution(
    clusters: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> dict[str, tuple[str, float, str]]:
    attribution: dict[str, tuple[str, float, str]] = {}
    best_candidate: dict[str, tuple[str, float]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("review_status") or "") == "rejected":
            continue
        cluster_id = str(candidate.get("face_cluster_id") or "")
        person_id = str(candidate.get("person_group_id") or "")
        confidence = _number_or_none(candidate.get("confidence")) or 0.0
        if not cluster_id or not person_id:
            continue
        current = best_candidate.get(cluster_id)
        if current is None or confidence > current[1]:
            best_candidate[cluster_id] = (person_id, confidence)
    for cluster_id, (person_id, confidence) in best_candidate.items():
        if confidence >= CLUSTER_ATTRIBUTION_MIN_CONFIDENCE:
            attribution[cluster_id] = (person_id, confidence, "candidate")
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        linked = str(cluster.get("linked_person_group_id") or "")
        if linked:
            attribution[str(cluster.get("id") or "")] = (linked, HUMAN_LINK_CONFIDENCE, "confirmed")
    return attribution


# --- persistence + claims -----------------------------------------------------


def _write_models(project: Path, models: list[dict[str, Any]]) -> None:
    import json

    path = project / PERSON_AGE_MODELS_FILENAME
    with path.open("w", encoding="utf-8") as handle:
        for model in models:
            handle.write(json.dumps(model, sort_keys=True, ensure_ascii=False) + "\n")


def _write_claims(
    project: Path,
    models: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    attribution: dict[str, tuple[str, float, str]],
    media_years: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    writer = DualWriter.open(
        project, artifact=PERSON_AGE_MODELS_FILENAME, producer="age-timeline/buffalo_l-genderage"
    )
    writer.supersede_previous()
    attribute_claims = 0
    for track in tracks:
        age = _number_or_none(track.get("age_estimate"))
        if age is None:
            continue
        span = track.get("span") or {}
        writer.write_row(
            track,
            kind="attribute",
            media_id=str(track.get("media_id") or "") or None,
            start_s=_number_or_none(span.get("start_s")),
            end_s=_number_or_none(span.get("end_s")),
            confidence=_number_or_none(track.get("mean_det_score")),
            assertion={
                "attribute": "age_estimate",
                "value": age,
                "sigma": CHILD_AGE_SIGMA if age <= CHILD_AGE_MAX else ADULT_AGE_SIGMA,
                "track_id": track.get("id"),
                "cluster_id": track.get("face_cluster_id") or None,
            },
        )
        attribute_claims += 1

    # age_anchor date claims only where no better date signal exists: media
    # with attributed, modeled people but no event/tape-level year estimate.
    models_by_person = {model["person_group_id"]: model for model in models}
    age_anchor_claims = 0
    anchored_media: set[str] = set()
    for track in tracks:
        media_id = str(track.get("media_id") or "")
        if not media_id or media_id in anchored_media or media_id in media_years:
            continue
        attributed = attribution.get(str(track.get("face_cluster_id") or ""))
        age = _number_or_none(track.get("age_estimate"))
        if not attributed or age is None:
            continue
        model = models_by_person.get(attributed[0])
        if not model:
            continue
        year = model["birth_year"] + age
        sigma = math.sqrt(
            float(model["sigma"]) ** 2
            + (CHILD_AGE_SIGMA if age <= CHILD_AGE_MAX else ADULT_AGE_SIGMA) ** 2
        )
        writer.write_row(
            track,
            kind="date",
            media_id=media_id,
            confidence=min(AGE_ANCHOR_MAX_CONFIDENCE, attributed[1] * 0.7),
            assertion={
                "origin": "age_anchor",
                "year": round(year, 1),
                "sigma": round(sigma, 2),
                "person_group_id": attributed[0],
                "track_id": track.get("id"),
            },
        )
        anchored_media.add(media_id)
        age_anchor_claims += 1

    return {
        "attribute_claims": attribute_claims,
        "age_anchor_claims": age_anchor_claims,
        "error": writer.error,
    }


# --- helpers ------------------------------------------------------------------


def _weighted_median(values: list[float], weights: list[float]) -> float:
    if not values:
        return 0.0
    if not weights or len(weights) != len(values):
        weights = [1.0] * len(values)
    pairs = sorted(zip(values, weights, strict=False))
    total = sum(weight for _value, weight in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= total / 2:
            return value
    return pairs[-1][0]


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "build_person_age_models",
    "load_person_age_models",
    "PERSON_AGE_MODELS_FILENAME",
]

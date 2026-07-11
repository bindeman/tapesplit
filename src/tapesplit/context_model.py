"""v2 context model (REFOUNDATION.md section 4, module M3).

v1 flattened two different ideas into one display string; this module keeps
them apart as *typed* context objects, decided at write time:

- **GeoContext** — physical containment (venue -> city -> region -> country),
  backed by an anchor: a verification vote, a confident geocode, or a human
  confirmation. Exists only for places whose geography is actually resolved.
- **EraContext** — a period of family life ("the Moscow years") with a year
  window and a residence region. Eras attach to *segments* of a tape, never
  to whole tapes: continuity carries an era until a break signal (non-content
  gap, language shift, overlay-date jump, or a named-place anchor), so one
  tape can span the family's move.

The contradiction guard lives here too: a context claim whose geographic
parent is disjoint from the place's anchored parent is never auto-acceptable
(`classify_context_candidate` -> ``contradiction=True``), and remediation of
the machine-cemented v1 corrections is a first-class operation
(`supersede_machine_place_context_corrections`).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapesplit.storage import read_jsonl

GEO_CONTEXTS_FILENAME = "geo_contexts.jsonl"
ERA_CONTEXTS_FILENAME = "era_contexts.jsonl"
SEGMENT_CONTEXTS_FILENAME = "segment_contexts.jsonl"

CONTEXT_KINDS = {"geo", "era"}

# Break-signal thresholds. A non-content range at least this long is a
# recording boundary; two anchored regions this far apart cannot be one
# continuous context.
NON_CONTENT_BREAK_S = 120.0
DISJOINT_DISTANCE_KM = 150.0

# Anchor sources, strongest first. Human decisions always outrank machines;
# verification votes (a model that examined the evidence) outrank a bare
# gazetteer hit; geocodes below GEOCODE_ANCHOR_MIN_CONFIDENCE or flagged
# context_suspect never anchor at all.
ANCHOR_BASIS_RANK = {"human_confirmed": 3, "verification_vote": 2, "geocode": 1}
GEOCODE_ANCHOR_MIN_CONFIDENCE = 0.6
VOTE_ANCHOR_MIN_CONFIDENCE = 0.7

# Contradicting candidates are preserved for review, never auto-accepted;
# their confidence is capped well below every place-context floor.
CONTRADICTION_CONFIDENCE_CAP = 0.4

_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}

_COUNTRY_ALIASES = {
    "united states": "us", "united states of america": "us", "usa": "us",
    "us": "us", "russia": "ru", "russian federation": "ru",
    "switzerland": "ch", "germany": "de", "france": "fr",
}


class ContextKindError(ValueError):
    """A context row violates the typed-context schema contract."""


def validate_context_kind(kind: str) -> None:
    if kind not in CONTEXT_KINDS:
        raise ContextKindError(f"unknown context kind: {kind!r}")


# ---------------------------------------------------------------------------
# Regions


def _fold(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return re.sub(r"\s+", " ", value).strip()


def parse_region(text: Any) -> dict[str, Any]:
    """Parse "Eugene, Oregon, United States" style region text.

    Returns {city, state, country} with best-effort assignment; unknown
    slots stay None. Single tokens are classified (country > US state > city).
    """

    parts = [part.strip() for part in str(text or "").split(",") if part.strip()]
    region: dict[str, Any] = {"city": None, "state": None, "country": None}
    if not parts:
        return region
    remaining = list(parts)
    last = _fold(remaining[-1])
    if last in _COUNTRY_ALIASES:
        region["country"] = _COUNTRY_ALIASES[last]
        remaining.pop()
    if remaining:
        candidate = _fold(remaining[-1])
        candidate_state = re.sub(r"\s+(oblast|krai|republic|region|county|state)$", "", candidate)
        if candidate in _US_STATES:
            region["state"] = candidate
            region.setdefault("country", None)
            region["country"] = region["country"] or "us"
            remaining.pop()
        elif candidate_state != candidate:
            region["state"] = candidate_state
            remaining.pop()
    if remaining:
        region["city"] = _fold(remaining[0]) or None
    # A lone token that is a US state ("Hawaii") or a known country arrives
    # here as city; promote it.
    if region["city"] and not region["state"] and not region["country"]:
        if region["city"] in _US_STATES:
            region["state"], region["city"] = region["city"], None
            region["country"] = "us"
        elif region["city"] in _COUNTRY_ALIASES:
            region["country"], region["city"] = _COUNTRY_ALIASES[region["city"]], None
    return region


def region_from_geocode(selected: dict[str, Any]) -> dict[str, Any]:
    return {
        "city": _fold(selected.get("city")) or None,
        "state": _fold(selected.get("state")) or None,
        "country": _COUNTRY_ALIASES.get(_fold(selected.get("country")), _fold(selected.get("country_code")) or None),
    }


def region_label(region: dict[str, Any]) -> str:
    """Human display for a region: "Eugene, Oregon" / "Moscow, Russia"."""

    names = {"us": "United States", "ru": "Russia", "ch": "Switzerland", "de": "Germany", "fr": "France"}
    city = str(region.get("city") or "").title()
    state = str(region.get("state") or "").title()
    country = names.get(str(region.get("country") or ""), str(region.get("country") or "").upper())
    if region.get("country") == "us":
        parts = [part for part in (city, state) if part]
    else:
        parts = [part for part in (city, country) if part] or [part for part in (state, country) if part]
    return ", ".join(parts)


def regions_disjoint(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_coords: tuple[float, float] | None = None,
    right_coords: tuple[float, float] | None = None,
) -> bool:
    """Whether two regions cannot contain one another.

    Different countries are disjoint; different states within one country
    are disjoint; matching or unknown slots are compatible (a bare
    "Oregon" is not disjoint from "Eugene, Oregon"). When both sides carry
    coordinates, distance is the tie-breaker for unknown admin slots.
    """

    lc, rc = left.get("country"), right.get("country")
    if lc and rc and lc != rc:
        return True
    ls, rs = left.get("state"), right.get("state")
    if ls and rs and ls != rs:
        return True
    if not (ls and rs) and left_coords and right_coords:
        if _haversine_km(left_coords, right_coords) > DISJOINT_DISTANCE_KM:
            return True
    return False


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
# Anchors


def load_anchor_lookup(project: Path) -> dict[str, dict[str, Any]]:
    """Best geographic anchor per folded place label, strongest source wins.

    Sources: human-confirmed corrections (rank 3), verification votes'
    ``actual_region`` (rank 2, support and refute alike — both carry the
    model's belief about the true region), confident non-suspect geocodes
    (rank 1). Group mapping happens later via `map_anchors_to_groups`.
    """

    by_label: dict[str, dict[str, Any]] = {}

    def offer(label_key: str, group_id: str | None, anchor: dict[str, Any]) -> None:
        del group_id
        if not label_key:
            return
        current = by_label.get(label_key)
        if current is None or _anchor_rank(anchor) > _anchor_rank(current):
            by_label[label_key] = anchor

    for row in read_jsonl(project / "place_geocodes.jsonl"):
        selected = row.get("selected") if isinstance(row.get("selected"), dict) else None
        confidence = float(row.get("confidence") or 0.0)
        if not selected or row.get("context_suspect") or confidence < GEOCODE_ANCHOR_MIN_CONFIDENCE:
            continue
        region = region_from_geocode(selected)
        if not (region.get("country") or region.get("state")):
            continue
        anchor = {
            "region": region,
            "lat": selected.get("lat"),
            "lng": selected.get("lng"),
            "basis": "geocode",
            "confidence": confidence,
            "source_id": row.get("key"),
        }
        offer(_fold(row.get("place_label")), row.get("place_group_id"), anchor)

    for row in read_jsonl(project / "verification_votes.jsonl"):
        if row.get("kind") != "place_context_geo":
            continue
        confidence = float(row.get("confidence") or 0.0)
        if confidence < VOTE_ANCHOR_MIN_CONFIDENCE:
            continue
        region = parse_region(row.get("actual_region"))
        if not (region.get("country") or region.get("state")):
            if row.get("verdict") == "refute":
                # The vote knows the attached context is wrong but its true
                # region doesn't parse to an admin region (continents,
                # "East Africa"). A veto anchor suppresses weaker anchors
                # without asserting any geography of its own.
                offer(
                    _fold(row.get("place")),
                    row.get("place_group_id"),
                    {
                        "region": {},
                        "veto": True,
                        "lat": None,
                        "lng": None,
                        "basis": "verification_vote",
                        "confidence": confidence,
                        "source_id": row.get("id") or row.get("place"),
                        "verdict": row.get("verdict"),
                        "actual_region_text": row.get("actual_region"),
                    },
                )
            continue
        anchor = {
            "region": region,
            "lat": None,
            "lng": None,
            "basis": "verification_vote",
            "confidence": confidence,
            "source_id": row.get("id") or row.get("place"),
            "verdict": row.get("verdict"),
        }
        offer(_fold(row.get("place")), row.get("place_group_id"), anchor)

    for row in read_jsonl(project / "corrections.jsonl"):
        if row.get("action") != "confirm_place" or row.get("superseded"):
            continue
        if str(row.get("reviewer") or "") in {"auto-pipeline", "bulk-suggestion", "review-ui-bulk", "clip-verifier"}:
            continue
        region = parse_region((row.get("payload") or {}).get("region"))
        if not (region.get("country") or region.get("state")):
            continue
        anchor = {
            "region": region,
            "lat": None,
            "lng": None,
            "basis": "human_confirmed",
            "confidence": 0.99,
            "source_id": row.get("target_id"),
        }
        offer(_fold((row.get("payload") or {}).get("label")), row.get("target_id"), anchor)

    return by_label


def map_anchors_to_groups(
    lookup: dict[str, dict[str, Any]],
    place_groups: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve the label-keyed anchor lookup onto place group ids."""

    anchors: dict[str, dict[str, Any]] = {}
    for group in place_groups:
        group_id = str(group.get("id") or "")
        best = None
        for alias in [group.get("label"), *(group.get("aliases") or [])]:
            candidate = lookup.get(_fold(alias))
            if candidate and (best is None or _anchor_rank(candidate) > _anchor_rank(best)):
                best = candidate
        if best:
            anchors[group_id] = best
    return anchors


def anchor_for_labels(lookup: dict[str, dict[str, Any]], labels: list[Any]) -> dict[str, Any] | None:
    """Strongest anchor among a set of place labels (event-level helper)."""

    best = None
    for label in labels:
        candidate = lookup.get(_fold(label))
        if candidate and (best is None or _anchor_rank(candidate) > _anchor_rank(best)):
            best = candidate
    return best


def _anchor_rank(anchor: dict[str, Any]) -> tuple[int, float]:
    return (ANCHOR_BASIS_RANK.get(str(anchor.get("basis")), 0), float(anchor.get("confidence") or 0.0))


def anchor_asserts_region(anchor: dict[str, Any] | None) -> bool:
    if not anchor or anchor.get("veto"):
        return False
    region = anchor.get("region") or {}
    return bool(region.get("country") or region.get("state"))


def build_geo_contexts(place_groups: list[dict[str, Any]], anchors: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for group in place_groups:
        anchor = anchors.get(str(group.get("id") or ""))
        if not anchor_asserts_region(anchor):
            continue
        region = anchor["region"]
        chain = [part for part in (group.get("label"), region_label(region)) if part]
        rows.append(
            {
                "id": f"geo_context_{group['id']}",
                "kind": "geo",
                "place_group_id": group.get("id"),
                "place_label": group.get("label"),
                "region": region,
                "region_label": region_label(region),
                "chain": chain,
                "lat": anchor.get("lat"),
                "lng": anchor.get("lng"),
                "basis": anchor.get("basis"),
                "confidence": anchor.get("confidence"),
                "source_id": anchor.get("source_id"),
                "review_status": "unreviewed" if anchor.get("basis") != "geocode" else "needs_review",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Segments (break-signal continuity boundaries)


def build_segment_contexts(
    project: Path,
    events: list[dict[str, Any]],
    *,
    event_media: dict[str, list[str]],
    event_years: dict[str, list[int]],
    event_regions: dict[str, dict[str, Any]],
    event_languages: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Split each media's events into continuity segments.

    Break signals between adjacent events: a qualifying non-content gap, a
    language shift, an overlay-date day jump, or anchored regions that are
    geographically disjoint. Returns (segment rows, event_id -> segment_id).
    """

    non_content: dict[str, list[tuple[float, float]]] = {}
    for row in read_jsonl(project / "non_content_ranges.jsonl"):
        media_id = str(row.get("source_media_id") or row.get("source_video_id") or "")
        start, end = row.get("start_s"), row.get("end_s")
        if media_id and start is not None and end is not None and float(end) - float(start) >= NON_CONTENT_BREAK_S:
            non_content.setdefault(media_id, []).append((float(start), float(end)))

    languages = event_languages or {}
    by_media: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        media_ids = event_media.get(str(event.get("id") or ""), [])
        if len(media_ids) == 1 and event.get("start_s") is not None:
            by_media.setdefault(media_ids[0], []).append(event)

    segments: list[dict[str, Any]] = []
    assignment: dict[str, str] = {}
    for media_id, media_events in sorted(by_media.items()):
        ordered = sorted(media_events, key=lambda row: float(row.get("start_s") or 0.0))
        current: list[dict[str, Any]] = []
        breaks: list[str] = []

        def flush(reason: str | None) -> None:
            nonlocal current
            if current:
                segment_id = f"segment_{media_id}_{len([s for s in segments if s['media_id'] == media_id]) + 1:03d}"
                segments.append(
                    {
                        "id": segment_id,
                        "media_id": media_id,
                        "start_s": float(current[0].get("start_s") or 0.0),
                        "end_s": float(current[-1].get("end_s") or current[-1].get("start_s") or 0.0),
                        "canonical_event_ids": [str(row.get("id")) for row in current],
                        "break_after": reason,
                    }
                )
                for row in current:
                    assignment[str(row.get("id"))] = segment_id
            current = []

        previous: dict[str, Any] | None = None
        for event in ordered:
            reason = None
            if previous is not None:
                reason = _break_reason(
                    previous,
                    event,
                    media_id,
                    non_content=non_content,
                    event_years=event_years,
                    event_regions=event_regions,
                    languages=languages,
                )
            if reason:
                breaks.append(reason)
                flush(reason)
            current.append(event)
            previous = event
        flush(None)
    return segments, assignment


def _break_reason(
    left: dict[str, Any],
    right: dict[str, Any],
    media_id: str,
    *,
    non_content: dict[str, list[tuple[float, float]]],
    event_years: dict[str, list[int]],
    event_regions: dict[str, dict[str, Any]],
    languages: dict[str, str],
) -> str | None:
    left_id, right_id = str(left.get("id")), str(right.get("id"))
    left_end = float(left.get("end_s") or left.get("start_s") or 0.0)
    right_start = float(right.get("start_s") or 0.0)
    for start, end in non_content.get(media_id, []):
        if left_end <= start and end <= right_start:
            return "non_content_gap"
    left_region, right_region = event_regions.get(left_id), event_regions.get(right_id)
    if left_region and right_region and regions_disjoint(left_region, right_region):
        return "anchored_region_change"
    left_years, right_years = event_years.get(left_id) or [], event_years.get(right_id) or []
    if left_years and right_years and min(abs(a - b) for a in left_years for b in right_years) >= 1:
        return "overlay_date_jump"
    left_lang, right_lang = languages.get(left_id), languages.get(right_id)
    if left_lang and right_lang and left_lang != right_lang:
        return "language_shift"
    return None


# ---------------------------------------------------------------------------
# Eras (residence-by-era)


def build_era_contexts(
    *,
    residence_votes: list[tuple[int, dict[str, Any], str]],
    extend_last_to: int | None = None,
) -> list[dict[str, Any]]:
    """Cluster (year, region, basis) residence votes into eras.

    Adjacent years with the same dominant region merge into one era; the
    move year is the boundary. School anchors are daily-life evidence and
    dominate; a year with conflicting evidence takes the majority and notes
    the conflict.
    """

    vote_weights = {"school_anchor": 2.0, "residence_role": 1.0, "capture_window": 0.5}

    by_year: dict[int, list[tuple[dict[str, Any], str]]] = {}
    for year, region, basis in residence_votes:
        if region.get("country") or region.get("state"):
            by_year.setdefault(int(year), []).append((region, basis))

    year_regions: dict[int, dict[str, Any]] = {}
    conflicts: dict[int, list[str]] = {}
    for year, votes in by_year.items():
        tally: dict[str, dict[str, Any]] = {}
        for region, basis in votes:
            key = region_label(region) or json.dumps(region, sort_keys=True)
            weight = vote_weights.get(basis, 1.0)
            entry = tally.setdefault(key, {"region": region, "weight": 0.0})
            entry["weight"] += weight
        winner = max(tally.values(), key=lambda entry: entry["weight"])
        year_regions[year] = winner["region"]
        if len(tally) > 1:
            conflicts[year] = sorted(tally)

    eras: list[dict[str, Any]] = []
    for year in sorted(year_regions):
        region = year_regions[year]
        label = region_label(region)
        if eras and eras[-1]["residence_label"] == label and year - eras[-1]["end_year"] <= 2:
            eras[-1]["end_year"] = year
        else:
            eras.append(
                {
                    "kind": "era",
                    "residence_label": label,
                    "residence_region": region,
                    "start_year": year,
                    "end_year": year,
                }
            )
    if eras and extend_last_to is not None and extend_last_to > eras[-1]["end_year"]:
        # A residence persists until there is evidence of a move; the final
        # era extends to the archive's horizon under that stated assumption.
        eras[-1]["end_year"] = extend_last_to
        eras[-1]["persistence_assumption"] = True
    for index, era in enumerate(eras, start=1):
        era["id"] = f"era_context_{index:03d}"
        era["label"] = f"{era['residence_label']} years"
        era["conflict_years"] = sorted(
            year for year in conflicts if era["start_year"] <= year <= era["end_year"]
        )
        era["review_status"] = "needs_review" if era["conflict_years"] or era.get("persistence_assumption") else "unreviewed"
        validate_context_kind(era["kind"])
    return eras


def era_for_year(eras: list[dict[str, Any]], year: int | None) -> dict[str, Any] | None:
    if year is None:
        return None
    for era in eras:
        if era["start_year"] <= year <= era["end_year"]:
            return era
    return None


# ---------------------------------------------------------------------------
# Candidate classification (the contradiction guard)


def classify_context_candidate(
    *,
    basis: str,
    group_anchor: dict[str, Any] | None,
    parent_anchor: dict[str, Any] | None,
    confidence: float,
) -> dict[str, Any]:
    """Type a place-context candidate and apply the contradiction guard.

    Direct geographic bases stay ``geo``; continuity/nearby inheritance is
    ``era``. A candidate whose parent region is disjoint from the group's own
    anchored region is a contradiction: confidence capped, never
    auto-acceptable, both hypotheses preserved for review.
    """

    kind = "geo" if "direct" in basis or basis == "same_event_place_context" else "era"
    validate_context_kind(kind)
    result = {
        "context_kind": kind,
        "contradiction": False,
        "confidence": confidence,
        "notes": [],
    }
    if not anchor_asserts_region(group_anchor):
        group_anchor = None
    if not anchor_asserts_region(parent_anchor):
        parent_anchor = None
    if group_anchor and parent_anchor:
        left, right = group_anchor["region"], parent_anchor["region"]
        left_coords = _coords(group_anchor)
        right_coords = _coords(parent_anchor)
        if regions_disjoint(left, right, left_coords=left_coords, right_coords=right_coords):
            result["contradiction"] = True
            result["confidence"] = min(confidence, CONTRADICTION_CONFIDENCE_CAP)
            result["notes"].append(
                f"Anchored to {region_label(left)} ({group_anchor.get('basis')}), which is disjoint from "
                f"{region_label(right)} — kept for review with both hypotheses, never auto-accepted."
            )
    return result


def _coords(anchor: dict[str, Any]) -> tuple[float, float] | None:
    lat, lng = anchor.get("lat"), anchor.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return (float(lat), float(lng))
    return None


# ---------------------------------------------------------------------------
# Remediation


def supersede_machine_place_context_corrections(project: Path, *, reason: str = "m3_context_v2") -> dict[str, Any]:
    """Mark every machine-made confirm_place_context correction superseded.

    They were all reviewer=auto-pipeline (nothing human is lost); replay
    skips superseded rows, so re-derivation under the v2 model starts from a
    clean slate instead of re-cementing v1's conflations.
    """

    path = project / "corrections.jsonl"
    rows = read_jsonl(path)
    stamped = datetime.now(timezone.utc).isoformat()
    superseded = 0
    for row in rows:
        if (
            row.get("action") == "confirm_place_context"
            and str(row.get("reviewer") or "") in {"auto-pipeline", "bulk-suggestion", "review-ui-bulk"}
            and not row.get("superseded")
        ):
            row["superseded"] = True
            row["superseded_by"] = reason
            row["superseded_at"] = stamped
            superseded += 1
    if superseded:
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
    return {"superseded": superseded, "total": len(rows)}

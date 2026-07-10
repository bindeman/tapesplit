"""v2 date model (REFOUNDATION.md section 5, module M2).

Every date reference carries an origin from a fixed taxonomy, and every
medium carries a plausible capture window fused from overlay datestamps,
narrated day-precision dates, and (down-weighted) container metadata. A date
far outside its media's window is auto-classified ``narrated_historical``
even when the extractor missed it; a date that cannot be classified routes
to ``resolve_date`` review instead of a chronology bucket.

The origin enum is a schema contract: ``origin`` is either one of
``DATE_ORIGINS`` or ``None``, and ``None`` is only legal on a group that is
flagged for review (`validate_origin`). No layer may drop the qualifier
without failing validation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tapesplit.storage import read_jsonl

CAPTURE_WINDOWS_FILENAME = "capture_windows.jsonl"

DATE_ORIGINS = {
    "overlay_datestamp",
    "narrated_current",
    "narrated_historical",
    "handwritten",
    "era_estimate",
    "age_anchor",
    "exif_capture",
    "exif_scan",
}

# Window derivation: strong per-media year evidence padded by one year on
# each side. Container (EXIF/mp4-atom) dates further than this many years
# from the newest strong evidence are digitization/export stamps
# (origin exif_scan), never capture evidence.
WINDOW_PAD_YEARS = 1
EXIF_SCAN_GAP_YEARS = 3
ARCHIVE_ENVELOPE_CONFIDENCE = 0.3
MEDIA_WINDOW_CONFIDENCE = 0.75

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Camcorder overlay datestamp: "SEP. 9 1998", "APR 27 2002", "NOV 4 2004".
_OVERLAY_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
# A lone plausible year on screen ("1998") still anchors the window, at
# lower weight; "12005" and other digit runs must not match.
_OVERLAY_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


class OriginContractError(ValueError):
    """A date group violates the origin schema contract."""


def validate_origin(origin: str | None, *, needs_review: bool) -> None:
    if origin is None:
        if not needs_review:
            raise OriginContractError("origin=None requires the group to be flagged for review")
        return
    if origin not in DATE_ORIGINS:
        raise OriginContractError(f"unknown date origin: {origin!r}")


def overlay_datestamps(project: Path) -> dict[str, list[dict[str, Any]]]:
    """Camcorder datestamp overlays per media, from OCR observations."""

    by_media: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(project / "visual_text_observations.jsonl"):
        media_id = str(row.get("source_media_id") or row.get("source_video_id") or "")
        text = str(row.get("text") or "")
        if not media_id or not text:
            continue
        entry = _parse_overlay_text(text)
        if entry is None:
            continue
        entry.update({"start_s": row.get("start_s"), "end_s": row.get("end_s"), "text": text})
        by_media.setdefault(media_id, []).append(entry)
    return by_media


def _parse_overlay_text(text: str) -> dict[str, Any] | None:
    match = _OVERLAY_DATE_RE.search(text)
    if match:
        month = _MONTHS[match.group(1).lower()[:3]]
        day = int(match.group(2))
        year = int(match.group(3))
        if 1 <= day <= 31:
            return {
                "date_value": f"{year:04d}-{month:02d}-{day:02d}",
                "year": year,
                "precision": "day",
            }
    year_match = _OVERLAY_YEAR_RE.fullmatch(text.strip())
    if year_match:
        return {"date_value": year_match.group(1), "year": int(year_match.group(1)), "precision": "year"}
    return None


def container_date_years(project: Path) -> dict[str, list[dict[str, Any]]]:
    """Container/EXIF date candidates per media (classified later)."""

    by_media: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(project / "media_metadata.jsonl"):
        media_id = str(row.get("source_media_id") or row.get("source_video_id") or row.get("id") or "")
        if not media_id:
            continue
        for candidate in row.get("date_candidates") or []:
            iso = str(candidate.get("iso") or "")
            if len(iso) >= 4 and iso[:4].isdigit():
                by_media.setdefault(media_id, []).append(
                    {
                        "year": int(iso[:4]),
                        "field": candidate.get("field"),
                        "confidence": candidate.get("confidence"),
                    }
                )
    return by_media


def build_capture_windows(
    project: Path,
    *,
    narrated_day_years: dict[str, list[int]] | None = None,
    media_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-media plausible capture windows from overlay + narrated + container evidence.

    A media row with no strong evidence of its own falls back to the archive
    envelope (the union of every media's strong years) at reduced confidence.
    """

    overlays = overlay_datestamps(project)
    containers = container_date_years(project)
    narrated = narrated_day_years or {}

    all_media = list(
        dict.fromkeys(
            (media_ids or [])
            + sorted(overlays)
            + sorted(narrated)
            + sorted(containers)
        )
    )

    observations_by_media: dict[str, list[dict[str, Any]]] = {}
    for media_id in all_media:
        # Only day-precision camcorder datestamps are trusted core evidence.
        # A bare year on screen is as likely historical signage ("1900" on an
        # Alaska plaque) as a camcorder year — it corroborates, never seeds
        # alone unless it belongs to the media's majority year cluster.
        observations = [
            {"year": entry["year"], "basis": "overlay_datestamp", "detail": entry.get("text")}
            for entry in overlays.get(media_id, [])
            if entry.get("precision") == "day"
        ]
        weak = [
            {"year": entry["year"], "basis": "overlay_year", "detail": entry.get("text")}
            for entry in overlays.get(media_id, [])
            if entry.get("precision") != "day"
        ] + [{"year": year, "basis": "narrated_day_date"} for year in narrated.get(media_id, [])]

        # Weak evidence (bare on-screen years, narrated full dates) joins the
        # window only near the trusted core — a narrated "June 6 1912" must
        # never widen a 2004 window. With no day-precision overlays at all,
        # the majority cluster of weak years seeds the core so a lone outlier
        # still cannot drag the window.
        core_years = sorted({obs["year"] for obs in observations}) or _majority_years(
            [obs["year"] for obs in weak]
        )
        for obs in weak:
            if core_years and abs(obs["year"] - _nearest(core_years, obs["year"])) <= EXIF_SCAN_GAP_YEARS:
                observations.append(obs)
            else:
                observations.append({**obs, "excluded": True})
        observations_by_media[media_id] = observations

    envelope_years = sorted(
        {
            obs["year"]
            for entries in observations_by_media.values()
            for obs in entries
            if not obs.get("excluded")
        }
    )

    windows: list[dict[str, Any]] = []
    for media_id in all_media:
        observations = list(observations_by_media.get(media_id, []))
        strong_years = sorted({obs["year"] for obs in observations if not obs.get("excluded")})

        # Container dates corroborated by strong evidence count as capture
        # metadata; the rest are digitization/export stamps (exif_scan).
        for candidate in containers.get(media_id, []):
            year = candidate["year"]
            reference = strong_years or envelope_years
            if reference and abs(year - _nearest(reference, year)) <= EXIF_SCAN_GAP_YEARS:
                observations.append({"year": year, "basis": "exif_capture", "detail": candidate.get("field")})
                strong_years = sorted(set(strong_years) | {year})
            else:
                observations.append(
                    {"year": year, "basis": "exif_scan", "detail": candidate.get("field"), "excluded": True}
                )

        if strong_years:
            basis = sorted({obs["basis"] for obs in observations if not obs.get("excluded")})
            start_year = min(strong_years) - WINDOW_PAD_YEARS
            end_year = max(strong_years) + WINDOW_PAD_YEARS
            confidence = MEDIA_WINDOW_CONFIDENCE
        elif envelope_years:
            basis = ["archive_envelope"]
            start_year = min(envelope_years) - WINDOW_PAD_YEARS
            end_year = max(envelope_years) + WINDOW_PAD_YEARS
            confidence = ARCHIVE_ENVELOPE_CONFIDENCE
        else:
            continue

        windows.append(
            {
                "id": f"capture_window_{media_id}",
                "media_id": media_id,
                "start_year": start_year,
                "end_year": end_year,
                "basis": basis,
                "observations": observations,
                "confidence": confidence,
                "review_status": "unreviewed",
                "notes": (
                    ["No direct evidence on this media; window inherited from the archive envelope."]
                    if basis == ["archive_envelope"]
                    else []
                ),
            }
        )
    return windows


def window_check(year: int | None, media_ids: list[str], windows_by_media: dict[str, dict[str, Any]]) -> str:
    """'inside' | 'outside' | 'no_window' for a year against its media's windows."""

    if year is None:
        return "no_window"
    checked = [windows_by_media[m] for m in media_ids if m in windows_by_media]
    if not checked:
        return "no_window"
    for window in checked:
        if window["start_year"] <= year <= window["end_year"]:
            return "inside"
    return "outside"


def classify_date_group(
    parsed: dict[str, Any],
    *,
    media_ids: list[str],
    windows_by_media: dict[str, dict[str, Any]],
    overlays_by_media: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Assign origin + exclusion + review routing to a parsed date candidate.

    Returns {origin, excluded_as_event_date, capture_window_check,
    needs_review, classification_notes}. Origin=None (unclassifiable) always
    sets needs_review, per the schema contract.
    """

    date_value = parsed.get("date_value")
    precision = parsed.get("precision")
    year = _year_of(date_value)
    check = window_check(year, media_ids, windows_by_media)
    notes: list[str] = []

    if precision == "day":
        matches_overlay = any(
            entry.get("date_value") == date_value
            for media_id in media_ids
            for entry in overlays_by_media.get(media_id, [])
        )
        if check == "outside":
            origin, excluded, needs_review = "narrated_historical", True, False
            notes.append(
                f"Full date {date_value} falls outside the media capture window; treated as historical context."
            )
        else:
            origin = "overlay_datestamp" if matches_overlay else "narrated_current"
            excluded = False
            needs_review = check == "no_window" and not matches_overlay
            if matches_overlay:
                notes.append("Corroborated by an on-screen camcorder datestamp.")
            if needs_review:
                notes.append("No capture window available to corroborate this date.")
    elif precision == "year":
        if check == "outside":
            origin, excluded, needs_review = "narrated_historical", True, False
            notes.append(f"Year {date_value} is outside the media capture window; historical context.")
        elif check == "inside":
            origin, excluded, needs_review = "narrated_current", True, True
            notes.append(
                "Year matches the capture window but a bare year is not safe as an event date; confirm in review."
            )
        else:
            origin, excluded, needs_review = None, True, True
            notes.append("Bare year with no capture window; cannot classify without review.")
    elif precision == "decade":
        origin, excluded, needs_review = "narrated_historical", True, False
        notes.append("Decade references are storytelling context, not recording dates.")
    else:
        origin, excluded, needs_review = None, True, True
        notes.append("Unparseable date clue; needs review.")

    validate_origin(origin, needs_review=needs_review)
    return {
        "origin": origin,
        "excluded_as_event_date": excluded,
        "capture_window_check": check,
        "needs_review": needs_review,
        "classification_notes": notes,
    }


def legacy_source_kind(origin: str | None, excluded: bool) -> str:
    """Map the v2 origin onto the pre-M2 source_kind vocabulary."""

    if origin == "narrated_historical":
        return "mentioned_historical_date"
    if not excluded:
        return "event_date_candidate"
    return "date_candidate"


def _year_of(date_value: Any) -> int | None:
    text = str(date_value or "")
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _nearest(years: list[int], year: int) -> int:
    return min(years, key=lambda item: abs(item - year))


def _majority_years(years: list[int]) -> list[int]:
    """The largest cluster of years within EXIF_SCAN_GAP_YEARS of each other."""

    if not years:
        return []
    unique = sorted(set(years))
    best: list[int] = []
    for anchor in unique:
        cluster = [year for year in unique if abs(year - anchor) <= EXIF_SCAN_GAP_YEARS]
        if len(cluster) > len(best):
            best = cluster
    return best

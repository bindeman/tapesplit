from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tapesplit.costs import ApiUsage, append_api_usage, estimate_api_cost_usd
from tapesplit.env import load_dotenv


@dataclass(frozen=True)
class GoogleMapsConfig:
    api_key: str | None
    enabled: bool
    daily_budget_usd: float

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.enabled and self.daily_budget_usd > 0)


def load_google_maps_config(env_path: Path | None = None) -> GoogleMapsConfig:
    load_dotenv(env_path)
    return GoogleMapsConfig(
        api_key=_empty_to_none(os.environ.get("GOOGLE_MAPS_API_KEY")),
        enabled=_bool(os.environ.get("GOOGLE_MAPS_ENABLED")),
        daily_budget_usd=_float(os.environ.get("GOOGLE_MAPS_DAILY_BUDGET_USD")),
    )


def check_google_maps_config(env_path: Path | None = None) -> dict:
    config = load_google_maps_config(env_path)
    return {
        "google_maps_api_key": bool(config.api_key),
        "google_maps_enabled": config.enabled,
        "google_maps_daily_budget_usd": config.daily_budget_usd,
        "google_maps_usable": config.usable,
    }


def build_place_query(
    name: str,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
) -> str:
    parts = [name, city, region, country]
    return " ".join(part.strip() for part in parts if part and part.strip())


def geocode_candidate(
    *,
    name: str,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
    project_dir: Path | None = None,
    allow_api: bool = False,
    max_results: int = 5,
) -> dict[str, Any]:
    query = build_place_query(name, city=city, region=region, country=country)
    config = load_google_maps_config()
    base = {
        "query": query,
        "provider": "google_maps",
        "api_called": False,
        "reason": None,
        "candidates": [],
    }
    if not allow_api:
        base["reason"] = "dry_run; pass --allow-api and enable Google Maps budget to call provider"
        return base
    if not config.usable:
        base["reason"] = "google maps disabled or daily budget is zero"
        return base

    results = _google_places_text_search(config.api_key or "", query=query, max_results=max_results)
    if project_dir is not None:
        append_api_usage(
            project_dir.expanduser().resolve(),
            ApiUsage(
                provider="google_maps",
                service="places_text_search",
                operation="geocode_candidate",
                units={"requests": 1},
                estimated_cost_usd=estimate_api_cost_usd(
                    provider="google_maps",
                    service="places_text_search",
                    units={"requests": 1},
                ),
                metadata={"query": query, "max_results": max_results},
            ),
        )
    base["api_called"] = True
    base["candidates"] = results[:max_results]
    return base


def _google_places_text_search(api_key: str, query: str, max_results: int) -> list[dict[str, Any]]:
    url = "https://places.googleapis.com/v1/places:searchText"
    field_mask = "places.id,places.displayName,places.formattedAddress,places.location,places.types"
    payload = {"textQuery": query, "maxResultCount": max_results}
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": field_mask,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google Places request failed ({exc.code}): {detail}") from exc

    payload = json.loads(body)
    candidates = []
    for place in payload.get("places", []):
        display = place.get("displayName") or {}
        location = place.get("location") or {}
        candidates.append(
            {
                "provider_place_id": place.get("id"),
                "name": display.get("text"),
                "formatted_address": place.get("formattedAddress"),
                "lat": location.get("latitude"),
                "lng": location.get("longitude"),
                "types": place.get("types", []),
            }
        )
    return candidates


# --------------------------------------------------------------------------
# Nominatim (OpenStreetMap) provider + project-level batch geocoding.
#
# Free, no key, 1 req/s politeness with a descriptive User-Agent. Results are
# cached durably in place_geocodes.jsonl (keyed by normalized query) so
# rebuilds never re-spend requests; export-visualization joins the cache back
# onto place groups by normalized label.

GEOCODES_FILENAME = "place_geocodes.jsonl"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "TapeSplit/1.0 (family-archive tool; contact: you@example.com)"
NOMINATIM_SLEEP_S = 1.1

# Region families used to spot geographically contradictory scopes (#12
# victims). A scope naming tokens from two DIFFERENT families is disjoint
# ("Moscow, Oregon"); tokens within one family are coherent ("Eugene,
# Oregon"). Suspect scopes geocode by bare place name, flagged context_suspect.
_REGION_FAMILIES: dict[str, set[str]] = {
    "oregon": {"oregon", "eugene", "portland", "florence"},
    "idaho": {"idaho"},
    "wisconsin": {"wisconsin", "madison"},
    "hawaii": {"hawaii", "hilo"},
    "alaska": {"alaska"},
    "california": {"california", "malibu"},
    "florida": {"florida"},
    "illinois": {"illinois", "chicago"},
    "washington": {"washington"},
    # "moscow" maps to the russia family: the archive's Moscow ambiguity
    # (Russia vs Idaho) makes any moscow+other-family scope untrustworthy.
    "russia": {"russia", "moscow", "kolomna", "ryazan"},
    "switzerland": {"switzerland", "davos"},
}


def normalized_geocode_key(query: str) -> str:
    return " ".join(query.casefold().split())


def nominatim_search(query: str, *, limit: int = 3, timeout: int = 30) -> list[dict[str, Any]]:
    from urllib.parse import urlencode

    params = urlencode(
        {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 1, "accept-language": "en"}
    )
    request = Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": NOMINATIM_USER_AGENT},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    results = []
    for row in payload:
        address = row.get("address") or {}
        results.append(
            {
                "provider_place_id": str(row.get("place_id")),
                "name": row.get("name") or row.get("display_name"),
                "formatted_address": row.get("display_name"),
                "lat": _float(row.get("lat")),
                "lng": _float(row.get("lon")),
                "type": row.get("type"),
                "importance": row.get("importance"),
                "city": address.get("city") or address.get("town") or address.get("village"),
                "state": address.get("state"),
                "country": address.get("country"),
                "country_code": address.get("country_code"),
            }
        )
    return results


def _scope_regions(place: dict[str, Any]) -> list[str]:
    text = " ".join(
        str(value)
        for value in [place.get("scope_label"), *(place.get("parent_place_labels") or [])]
        if value
    ).casefold()
    families = {
        family
        for family, tokens in _REGION_FAMILIES.items()
        if any(token in text for token in tokens)
    }
    return sorted(families)


def _context_suspect(place: dict[str, Any]) -> bool:
    # Tokens from two different region families in one scope is the known #12
    # conflation signature ("Moscow, Oregon"; "Alaska, Davos, Switzerland").
    return len(_scope_regions(place)) >= 2


def _place_queries(place: dict[str, Any]) -> list[str]:
    label = str(place.get("label") or "").strip()
    if not label:
        return []
    if _context_suspect(place):
        return [label]
    hints = [str(item) for item in (place.get("parent_place_labels") or []) if item]
    scope = str(place.get("scope_label") or "").strip()
    queries = []
    if hints:
        queries.append(" ".join([label, *hints[:2]]))
    elif scope and scope.casefold() != label.casefold():
        queries.append(f"{label} {scope}")
    queries.append(label)
    seen: set[str] = set()
    unique = []
    for query in queries:
        key = normalized_geocode_key(query)
        if key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


def _result_confidence(results: list[dict[str, Any]]) -> float:
    if not results:
        return 0.0
    top = results[0]
    importance = float(top.get("importance") or 0.3)
    base = 0.45 + min(importance, 0.45)
    if len(results) == 1:
        base += 0.08
    return round(min(base, 0.95), 3)


def geocode_project_places(
    project: Path,
    *,
    sleep_s: float = NOMINATIM_SLEEP_S,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Geocode named place candidates via Nominatim into place_geocodes.jsonl."""
    import time

    from tapesplit.claim_store import DualWriter
    from tapesplit.storage import append_jsonl, read_jsonl

    project = project.expanduser().resolve()
    groups_path = project / "place_groups.jsonl"
    if not groups_path.exists():
        return {"skipped": "place_groups.jsonl missing; run build-groups first", "geocoded": 0}

    cache_path = project / GEOCODES_FILENAME
    cached = {row.get("key"): row for row in read_jsonl(cache_path)} if cache_path.exists() else {}

    places = [
        row
        for row in read_jsonl(groups_path)
        if row.get("kind") == "named_place_candidate" and row.get("label")
    ]
    writer = DualWriter.open(project, artifact=GEOCODES_FILENAME, producer="geocode/nominatim", run_id=None)

    geocoded = failed = skipped_cached = ambiguous = 0
    for place in places[: limit or len(places)]:
        queries = _place_queries(place)
        if not queries:
            continue
        primary_key = normalized_geocode_key(queries[0])
        if not force and primary_key in cached:
            skipped_cached += 1
            continue
        row: dict[str, Any] = {
            "key": primary_key,
            "place_label": place.get("label"),
            "place_group_id": place.get("id"),
            "queries": queries,
            "provider": "nominatim",
            "context_suspect": _context_suspect(place),
            "scope_regions": _scope_regions(place),
        }
        results: list[dict[str, Any]] = []
        try:
            for query in queries:
                results = nominatim_search(query)
                time.sleep(sleep_s)
                if results:
                    row["resolved_query"] = query
                    break
        except Exception as error:  # noqa: BLE001 - network failures are data
            row["error"] = f"{type(error).__name__}: {error}"
            failed += 1
            append_jsonl(cache_path, row)
            cached[primary_key] = row
            continue

        if results:
            top = results[0]
            row["selected"] = top
            row["alternatives"] = results[1:3]
            row["confidence"] = _result_confidence(results)
            if len(results) > 1:
                ambiguous += 1
            geocoded += 1
            writer.write_row(
                row,
                kind="place_link",
                media_id=None,
                confidence=row["confidence"],
                assertion={
                    "place_label": row["place_label"],
                    "lat": top["lat"],
                    "lng": top["lng"],
                    "formatted_address": top["formatted_address"],
                    "context_suspect": row["context_suspect"],
                },
            )
        else:
            row["selected"] = None
            row["confidence"] = 0.0
            failed += 1
        append_jsonl(cache_path, row)
        cached[primary_key] = row

    return {
        "places_considered": len(places),
        "geocoded": geocoded,
        "ambiguous": ambiguous,
        "failed": failed,
        "skipped_cached": skipped_cached,
        "output": str(cache_path),
        "claims_written": writer.claims_written,
        "claims_error": writer.error,
    }


def load_place_geocodes(project: Path) -> dict[str, dict[str, Any]]:
    """Geocode cache keyed by normalized place label (best row per label)."""
    from tapesplit.storage import read_jsonl

    path = project / GEOCODES_FILENAME
    if not path.exists():
        return {}
    by_label: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        label = normalized_geocode_key(str(row.get("place_label") or ""))
        if not label or not row.get("selected"):
            continue
        current = by_label.get(label)
        if current is None or (row.get("confidence") or 0) > (current.get("confidence") or 0):
            by_label[label] = row
    return by_label


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None) -> float:
    try:
        return float((value or "0").strip())
    except ValueError:
        return 0.0


if __name__ == "__main__":  # pragma: no cover - thin runner; CLI wiring follows
    import argparse

    parser = argparse.ArgumentParser(description="Batch-geocode named places via Nominatim")
    parser.add_argument("project", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    print(json.dumps(geocode_project_places(args.project, force=args.force, limit=args.limit), indent=2))

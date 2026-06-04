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

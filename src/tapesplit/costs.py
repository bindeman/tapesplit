from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapesplit.env import load_dotenv
from tapesplit.storage import append_jsonl
from tapesplit.storage import read_jsonl


@dataclass(frozen=True)
class LlmUsage:
    provider: str
    deployment: str
    operation: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: float | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class ApiUsage:
    provider: str
    service: str
    operation: str
    units: dict[str, float | int | str]
    estimated_cost_usd: float | None = None
    request_id: str | None = None
    metadata: dict[str, Any] | None = None


def estimate_llm_cost_usd(
    *,
    provider: str,
    deployment: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float | None:
    rates = load_cost_rates()
    provider_rates = rates.get(provider, {})
    deployment_rates = provider_rates.get(deployment, {})
    if not deployment_rates:
        return None

    input_per_1m = deployment_rates.get("input_per_1m")
    output_per_1m = deployment_rates.get("output_per_1m")
    # explicit null in the rates file means "no cached tier" — bill cached
    # tokens at the normal input rate, same as an absent key
    cached_input_per_1m = deployment_rates.get("cached_input_per_1m")
    if cached_input_per_1m is None:
        cached_input_per_1m = input_per_1m
    if input_per_1m is None or output_per_1m is None:
        return None

    billable_input_tokens = max(0, input_tokens - cached_input_tokens)
    cost = (
        (billable_input_tokens / 1_000_000) * float(input_per_1m)
        + (cached_input_tokens / 1_000_000) * float(cached_input_per_1m)
        + (output_tokens / 1_000_000) * float(output_per_1m)
    )
    return round(cost, 8)


def estimate_api_cost_usd(
    *,
    provider: str,
    service: str,
    units: dict[str, float | int | str],
) -> float | None:
    rates = load_cost_rates()
    provider_rates = rates.get(provider, {})
    service_rates = provider_rates.get(service, {})
    if not service_rates:
        return None

    cost = 0.0
    matched = False
    for unit_name, raw_value in units.items():
        if not isinstance(raw_value, (int, float)):
            continue
        value = float(raw_value)
        direct_rate = service_rates.get(f"{unit_name}_rate")
        per_1k_rate = service_rates.get(f"{unit_name}_per_1k")
        per_1m_rate = service_rates.get(f"{unit_name}_per_1m")
        if direct_rate is not None:
            cost += value * float(direct_rate)
            matched = True
        elif per_1k_rate is not None:
            cost += (value / 1_000) * float(per_1k_rate)
            matched = True
        elif per_1m_rate is not None:
            cost += (value / 1_000_000) * float(per_1m_rate)
            matched = True

    return round(cost, 8) if matched else None


def load_cost_rates() -> dict[str, dict[str, dict[str, float]]]:
    load_dotenv()
    path = Path("cost_rates.json")
    if not path.exists():
        path = Path("cost_rates.example.json")
    if path.exists():
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    return {}


def append_llm_usage(project_dir: Path, usage: LlmUsage) -> None:
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": usage.provider,
        "deployment": usage.deployment,
        "operation": usage.operation,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "request_id": usage.request_id,
    }
    append_jsonl(project_dir / "costs.llm.jsonl", record)


def append_api_usage(project_dir: Path, usage: ApiUsage) -> None:
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": usage.provider,
        "service": usage.service,
        "operation": usage.operation,
        "units": usage.units,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "request_id": usage.request_id,
        "metadata": usage.metadata or {},
    }
    append_jsonl(project_dir / "costs.api.jsonl", record)


def summarize_llm_usage(project_dir: Path) -> dict[str, Any]:
    import json

    path = project_dir / "costs.llm.jsonl"
    rows = []
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if raw_line.strip():
                rows.append(json.loads(raw_line))

    summary: dict[str, Any] = {
        "records": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unknown_cost_records": 0,
        "by_deployment": {},
    }
    for row in rows:
        input_tokens = int(row.get("input_tokens") or 0)
        output_tokens = int(row.get("output_tokens") or 0)
        cached_input_tokens = int(row.get("cached_input_tokens") or 0)
        cost = row.get("estimated_cost_usd")
        key = f"{row.get('provider')}:{row.get('deployment')}"
        bucket = summary["by_deployment"].setdefault(
            key,
            {
                "records": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "estimated_cost_usd": 0.0,
                "unknown_cost_records": 0,
            },
        )
        for target in (summary, bucket):
            target["records"] += 1
            target["input_tokens"] += input_tokens
            target["output_tokens"] += output_tokens
            target["cached_input_tokens"] += cached_input_tokens
            if cost is None:
                target["unknown_cost_records"] += 1
            else:
                target["estimated_cost_usd"] = round(
                    target["estimated_cost_usd"] + float(cost),
                    8,
                )
    return summary


def summarize_api_usage(project_dir: Path) -> dict[str, Any]:
    rows = _read_cost_rows(project_dir / "costs.api.jsonl")
    summary: dict[str, Any] = {
        "records": 0,
        "estimated_cost_usd": 0.0,
        "unknown_cost_records": 0,
        "by_service": {},
    }
    for row in rows:
        cost = row.get("estimated_cost_usd")
        key = f"{row.get('provider')}:{row.get('service')}"
        bucket = summary["by_service"].setdefault(
            key,
            {
                "records": 0,
                "estimated_cost_usd": 0.0,
                "unknown_cost_records": 0,
                "units": {},
            },
        )
        for target in (summary, bucket):
            target["records"] += 1
            if cost is None:
                target["unknown_cost_records"] += 1
            else:
                target["estimated_cost_usd"] = round(
                    target["estimated_cost_usd"] + float(cost),
                    8,
                )
        for unit_name, raw_value in (row.get("units") or {}).items():
            if isinstance(raw_value, (int, float)):
                bucket["units"][unit_name] = round(
                    float(bucket["units"].get(unit_name, 0.0)) + float(raw_value),
                    6,
                )
    return summary


def summarize_project_costs(project_dir: Path) -> dict[str, Any]:
    llm = summarize_llm_usage(project_dir)
    api = summarize_api_usage(project_dir)
    estimated_total = round(
        float(llm["estimated_cost_usd"]) + float(api["estimated_cost_usd"]),
        8,
    )
    return {
        "project": str(project_dir),
        "estimated_total_cost_usd": estimated_total,
        "unknown_cost_records": llm["unknown_cost_records"] + api["unknown_cost_records"],
        "llm": llm,
        "api": api,
    }


def estimate_project_twelvelabs_index_cost(project_dir: Path) -> dict[str, Any]:
    tapes_path = project_dir / "tapes.jsonl"
    rows = read_jsonl(tapes_path)
    videos = []
    total_duration_min = 0.0
    total_cost = 0.0
    unknown_cost_records = 0
    for row in rows:
        duration_s = float((row.get("probe") or {}).get("duration_s") or 0.0)
        duration_min = round(duration_s / 60.0, 6)
        units = {"duration_min": duration_min, "requests": 1}
        cost = estimate_api_cost_usd(
            provider="twelvelabs",
            service="index",
            units=units,
        )
        if cost is None:
            unknown_cost_records += 1
        else:
            total_cost += cost
        total_duration_min += duration_min
        videos.append(
            {
                "source_video_id": row.get("id"),
                "filename": row.get("filename"),
                "duration_min": duration_min,
                "estimated_cost_usd": cost,
            }
        )

    return {
        "project": str(project_dir),
        "provider": "twelvelabs",
        "service": "index",
        "video_count": len(videos),
        "duration_min": round(total_duration_min, 6),
        "estimated_cost_usd": round(total_cost, 8),
        "unknown_cost_records": unknown_cost_records,
        "videos": videos,
    }


def _read_cost_rows(path: Path) -> list[dict[str, Any]]:
    import json

    rows = []
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if raw_line.strip():
                rows.append(json.loads(raw_line))
    return rows

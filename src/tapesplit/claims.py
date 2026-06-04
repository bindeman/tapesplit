from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tapesplit.azure_openai_adapter import (
    chat_completion,
    deployment_for_alias,
    load_azure_openai_config,
)
from tapesplit.evidence import evidence_for_prompt
from tapesplit.storage import append_jsonl, read_jsonl, write_json


CLAIM_SCHEMA_HINT = {
    "claims": [
        {
            "subject": "event_001 or tape",
            "predicate": "event|person_mention|place_candidate|date_candidate|non_content|language|relationship_candidate",
            "value": "short value",
            "confidence": 0.0,
            "evidence_ids": ["ev_000001"],
            "review_status": "unreviewed|needs_review",
            "notes": "why this is supported and what remains uncertain",
        }
    ],
    "event_candidates": [
        {
            "title": "short title",
            "start_s": 0,
            "end_s": 0,
            "confidence": 0.0,
            "evidence_ids": ["ev_000001"],
            "summary": "grounded summary",
        }
    ],
    "summary": "brief grounded summary of what this evidence suggests",
}


def extract_claims(project_dir: Path, deployment_alias: str = "fast") -> dict:
    project = project_dir.expanduser().resolve()
    evidence = evidence_for_prompt(project)
    if not evidence:
        raise RuntimeError("no evidence found; run build-evidence first")
    raw_evidence = read_jsonl(project / "evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in raw_evidence if row.get("id")}

    config = load_azure_openai_config()
    deployment = deployment_for_alias(config, deployment_alias)
    prompt = _build_claim_prompt(evidence)
    response = chat_completion(
        deployment=deployment,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract conservative, evidence-grounded claims from old family video evidence. "
                    "Return only JSON. Do not invent facts. Use null or needs_review when uncertain."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=2200,
        temperature=0.0,
        response_format={"type": "json_object"},
        project_dir=project,
        operation="extract_claims",
    )
    content = (response.get("choices") or [{}])[0].get("message", {}).get("content") or "{}"
    payload = _parse_json_object(content)

    claims_path = project / "claims.jsonl"
    events_path = project / "events.jsonl"
    summaries_path = project / "summaries.jsonl"
    for path in [claims_path, events_path, summaries_path]:
        if path.exists():
            path.unlink()

    claims = payload.get("claims") if isinstance(payload.get("claims"), list) else []
    events = _normalize_event_candidates(
        payload.get("event_candidates") if isinstance(payload.get("event_candidates"), list) else [],
        evidence_by_id,
    )
    if not events:
        events = _events_from_event_claims(claims, evidence_by_id)
    for index, claim in enumerate(claims, start=1):
        append_jsonl(
            claims_path,
            {
                "id": f"claim_{index:06d}",
                "source": "azure_openai",
                "deployment": deployment,
                "review_status": claim.get("review_status") or "unreviewed",
                **claim,
            },
        )
    for index, event in enumerate(events, start=1):
        append_jsonl(
            events_path,
            {
                "id": f"event_{index:06d}",
                "source": "azure_openai",
                "deployment": deployment,
                "review_status": event.get("review_status") or "unreviewed",
                **event,
            },
        )
    append_jsonl(
        summaries_path,
        {
            "id": "summary_000001",
            "kind": "claim_extraction_summary",
            "source": "azure_openai",
            "deployment": deployment,
            "text": payload.get("summary") or "",
        },
    )
    write_json(project / "claim_extraction.raw.json", payload)
    return {
        "project": str(project),
        "deployment": deployment,
        "claims": len(claims),
        "events": len(events),
        "summary": payload.get("summary") or "",
    }


def _build_claim_prompt(evidence: list[dict[str, Any]]) -> str:
    return (
        "Extract claims from the evidence below. Requirements:\n"
        "- Every claim/event must cite evidence_ids from the list.\n"
        "- Event start_s and end_s must be numeric seconds, not mm:ss or hh:mm:ss labels.\n"
        "- Separate observed facts from hypotheses.\n"
        "- Treat metadata dates as digitization/export dates unless corroborated.\n"
        "- Treat location/trip context as possible context unless directly stated.\n"
        "- Do not create event_candidates for blank/static/blue-screen/no-signal ranges; those are already tracked separately.\n"
        "- Preserve Russian text meaning; translate or summarize in English when helpful.\n"
        "- Mark uncertain identity/location/date claims as needs_review.\n\n"
        f"Return JSON shaped like this:\n{json.dumps(CLAIM_SCHEMA_HINT, ensure_ascii=False)}\n\n"
        f"Evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
    )


def _normalize_event_candidates(
    events: list[Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = []
    for event in events:
        if not isinstance(event, dict):
            continue
        evidence_ids = [
            item
            for item in event.get("evidence_ids", [])
            if isinstance(item, str) and item in evidence_by_id
        ]
        cited_evidence = [evidence_by_id[item] for item in evidence_ids]
        if cited_evidence and all(row.get("kind") == "non_content_range" for row in cited_evidence):
            continue

        corrected = dict(event)
        corrected["evidence_ids"] = evidence_ids
        start_s, end_s = _range_from_evidence(cited_evidence)
        if start_s is not None:
            corrected["start_s"] = start_s
        else:
            corrected["start_s"] = _number_or_none(corrected.get("start_s"))
        if end_s is not None:
            corrected["end_s"] = end_s
        else:
            corrected["end_s"] = _number_or_none(corrected.get("end_s"))
        if (
            corrected.get("start_s") is not None
            and corrected.get("end_s") is not None
            and corrected["end_s"] < corrected["start_s"]
        ):
            corrected["end_s"] = corrected["start_s"]
        normalized.append(corrected)
    return normalized


def _events_from_event_claims(
    claims: list[Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events = []
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("predicate") != "event":
            continue
        evidence_ids = [
            item
            for item in claim.get("evidence_ids", [])
            if isinstance(item, str) and item in evidence_by_id
        ]
        if not evidence_ids:
            continue
        start_s, end_s = _range_from_evidence([evidence_by_id[item] for item in evidence_ids])
        title = str(claim.get("value") or "Untitled event")
        events.append(
            {
                "title": title,
                "start_s": start_s,
                "end_s": end_s,
                "confidence": claim.get("confidence"),
                "evidence_ids": evidence_ids,
                "summary": claim.get("notes") or title,
                "review_status": claim.get("review_status") or "unreviewed",
                "derived_from": "event_claim",
            }
        )
    return _normalize_event_candidates(events, evidence_by_id)


def _range_from_evidence(rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    starts = [_number_or_none(row.get("start_s")) for row in rows]
    ends = [_number_or_none(row.get("end_s")) for row in rows]
    starts = [item for item in starts if item is not None]
    ends = [item for item in ends if item is not None]
    if not starts and not ends:
        return None, None
    start_s = min(starts or ends)
    end_s = max(ends or starts)
    return round(start_s, 3), round(end_s, 3)


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            payload = json.loads(text[start : end + 1])
            return payload if isinstance(payload, dict) else {}
        raise

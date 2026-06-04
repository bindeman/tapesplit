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
from tapesplit.storage import append_jsonl, write_json


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
    events = payload.get("event_candidates") if isinstance(payload.get("event_candidates"), list) else []
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
        "- Separate observed facts from hypotheses.\n"
        "- Treat metadata dates as digitization/export dates unless corroborated.\n"
        "- Treat location/trip context as possible context unless directly stated.\n"
        "- Preserve Russian text meaning; translate or summarize in English when helpful.\n"
        "- Mark uncertain identity/location/date claims as needs_review.\n\n"
        f"Return JSON shaped like this:\n{json.dumps(CLAIM_SCHEMA_HINT, ensure_ascii=False)}\n\n"
        f"Evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
    )


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

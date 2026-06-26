from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


NON_FILMING_PLACE_STATUSES = {
    "ambiguous_place_reference",
    "mentioned_destination",
    "travel_plan",
}

STRONG_CONTEXT_ANCHOR_ROLES = {
    "spoken_location_anchor",
}

GENERIC_CONTEXT_ANCHOR_ROLES = {
    "generic_place_context",
}


def build_event_reconciliations(project_dir: Path) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}
    visibility = build_visibility_filter(project)
    events = [
        row
        for row in read_jsonl(project / "canonical_events.jsonl")
        if visibility.visible_row(row, evidence_by_id=evidence_by_id)
    ]
    alignments_by_event = {
        str(row.get("canonical_event_id")): row
        for row in read_jsonl(project / "event_alignments.jsonl")
        if row.get("canonical_event_id") and visibility.visible_row(row, evidence_by_id=evidence_by_id)
    }
    place_roles_by_event_label = _place_roles_by_event_label(project)

    output = project / "event_reconciliations.jsonl"
    if output.exists():
        output.unlink()

    rows = []
    for index, event in enumerate(events, start=1):
        row = reconcile_event(
            event,
            alignment=alignments_by_event.get(str(event.get("id") or "")),
            place_roles_by_event_label=place_roles_by_event_label,
            index=index,
        )
        rows.append(row)
        append_jsonl(output, row)

    return {
        "project": str(project),
        "output": str(output),
        "event_reconciliations": len(rows),
        "by_status": dict(sorted(Counter(str(row.get("reconciliation_status") or "unknown") for row in rows).items())),
    }


def reconcile_event(
    event: dict[str, Any],
    *,
    alignment: dict[str, Any] | None,
    place_roles_by_event_label: dict[tuple[str, str], dict[str, Any]] | None = None,
    index: int = 1,
) -> dict[str, Any]:
    event_id = str(event.get("id") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    original_title = str(event.get("title") or "Untitled event")
    original_summary = str(event.get("summary") or "")
    event_type = str(metadata.get("event_type") or event.get("event_type") or "")
    alignment = alignment or {}
    timing_status = str(alignment.get("timing_status") or "")
    context_anchors = _context_anchors(alignment)
    place_decisions = _place_decisions(
        event,
        alignment=alignment,
        context_anchors=context_anchors,
        place_roles_by_event_label=place_roles_by_event_label or {},
    )
    selected_places = [decision for decision in place_decisions if decision.get("decision") == "selected"]
    rejected_places = [decision for decision in place_decisions if decision.get("decision") == "rejected_as_filming_location"]
    source_ranges = alignment.get("source_ranges") or _event_source_ranges(event)
    relocated_evidence_ranges = alignment.get("suggested_source_ranges") or []
    selected_source_ranges = source_ranges

    should_retitle = _should_retitle(timing_status, context_anchors, rejected_places, original_title=original_title)
    if should_retitle:
        title = _title_from_context(context_anchors, event_type=event_type)
        title_status = "retitled_from_local_context"
        reconciliation_status = "corrected"
        confidence = _reconciled_confidence(context_anchors, rejected_places, alignment)
    elif timing_status == "model_only":
        title = original_title
        title_status = "model_title_unverified"
        reconciliation_status = "needs_evidence"
        confidence = min(0.45, float(alignment.get("support_score") or 0.0))
    elif rejected_places:
        title = original_title
        title_status = "original_title_kept_place_claims_filtered"
        reconciliation_status = "metadata_corrected"
        confidence = max(0.5, min(0.9, float(alignment.get("support_score") or event.get("confidence") or 0.0)))
    else:
        title = original_title
        title_status = "original_supported"
        reconciliation_status = "accepted"
        confidence = max(0.5, min(0.95, float(alignment.get("support_score") or event.get("confidence") or 0.0)))

    warnings = _reconciliation_warnings(
        timing_status=timing_status,
        title_status=title_status,
        rejected_places=rejected_places,
        alignment=alignment,
    )
    return {
        "id": f"event_reconciliation_{index:06d}",
        "canonical_event_id": event_id,
        "original_title": original_title,
        "reconciled_title": title,
        "title_status": title_status,
        "reconciliation_status": reconciliation_status,
        "confidence": round(confidence, 3),
        "original_summary": original_summary,
        "reconciled_summary": _reconciled_summary(
            original_summary,
            title_status=title_status,
            context_anchors=context_anchors,
            rejected_places=rejected_places,
        ),
        "event_type": event_type,
        "source_video_ids": _event_source_video_ids(event, alignment),
        "source_ranges": source_ranges,
        "selected_source_ranges": selected_source_ranges,
        "relocated_evidence_ranges": relocated_evidence_ranges,
        "selected_place_labels": _unique_items(decision.get("label") for decision in selected_places),
        "rejected_place_labels": _unique_items(decision.get("label") for decision in rejected_places),
        "place_decisions": place_decisions,
        "date_candidates": _metadata_list(event, "date_candidates"),
        "people": _metadata_list(event, "people"),
        "alignment_id": alignment.get("id"),
        "alignment_status": timing_status,
        "support_score": alignment.get("support_score"),
        "evidence_claim_statuses": _count_values(
            claim.get("status") for claim in alignment.get("evidence_claims") or [] if isinstance(claim, dict)
        ),
        "warnings": warnings,
        "signals": _reconciliation_signals(context_anchors, rejected_places, alignment),
        "review_status": "unreviewed" if reconciliation_status in {"accepted", "corrected"} else "needs_review",
    }


def _place_roles_by_event_label(project: Path) -> dict[tuple[str, str], dict[str, Any]]:
    roles = {}
    for row in read_jsonl(project / "event_place_roles.jsonl"):
        event_id = str(row.get("canonical_event_id") or "")
        label = str(row.get("normalized_label") or _normalize_key(row.get("label")) or "")
        if not event_id or not label:
            continue
        current = roles.get((event_id, label))
        if not current or float(row.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
            roles[(event_id, label)] = row
    return roles


def _context_anchors(alignment: dict[str, Any]) -> list[dict[str, Any]]:
    anchors = []
    seen = set()
    for item in alignment.get("transcript_context_anchors") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        role = str(item.get("role") or "")
        if not label or role not in STRONG_CONTEXT_ANCHOR_ROLES | GENERIC_CONTEXT_ANCHOR_ROLES:
            continue
        key = (label.casefold(), role)
        if key in seen:
            continue
        seen.add(key)
        anchors.append(item)
    return sorted(
        anchors,
        key=lambda row: (
            0 if row.get("role") in STRONG_CONTEXT_ANCHOR_ROLES else 1,
            -(float(row.get("confidence") or 0.0)),
            float(row.get("distance_to_event_s") or 0.0),
            str(row.get("label") or "").casefold(),
        ),
    )


def _place_decisions(
    event: dict[str, Any],
    *,
    alignment: dict[str, Any],
    context_anchors: list[dict[str, Any]],
    place_roles_by_event_label: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    event_id = str(event.get("id") or "")
    decisions: list[dict[str, Any]] = []
    entity_places = _entity_places(alignment)
    for place in _metadata_list(event, "place_candidates"):
        key = _normalize_key(place)
        entity = entity_places.get(key, {})
        place_role = entity.get("place_role") if isinstance(entity.get("place_role"), dict) else None
        place_role = place_role or place_roles_by_event_label.get((event_id, key), {})
        role = str(place_role.get("role") or entity.get("status") or "")
        include = place_role.get("include_in_place_groups")
        if role in NON_FILMING_PLACE_STATUSES or include is False:
            decision = "rejected_as_filming_location"
            reason = role or "non_filming_context"
        elif str(entity.get("status") or "") in {"direct_transcript", "weak_transcript"}:
            decision = "selected"
            reason = str(entity.get("status"))
        elif include is True and alignment.get("timing_status") not in {"possible_misaligned", "model_only"}:
            decision = "selected"
            reason = role or "place_role_included"
        else:
            decision = "candidate"
            reason = role or str(entity.get("status") or "model_candidate")
        decisions.append(
            {
                "label": place,
                "decision": decision,
                "role": role,
                "reason": reason,
                "source": "event_place_role" if place_role else "event_metadata",
                "confidence": place_role.get("confidence") or entity.get("confidence"),
                "evidence": _place_decision_evidence(entity, place_role),
            }
        )

    for anchor in context_anchors:
        decisions.append(
            {
                "label": anchor.get("label"),
                "decision": "selected",
                "role": anchor.get("role"),
                "reason": "local_transcript_context_anchor",
                "source": "transcript_context_anchor",
                "confidence": anchor.get("confidence"),
                "evidence": [
                    {
                        "transcript_id": anchor.get("transcript_id"),
                        "source_video_id": anchor.get("source_video_id"),
                        "start_s": anchor.get("start_s"),
                        "end_s": anchor.get("end_s"),
                        "text": anchor.get("text"),
                    }
                ],
            }
        )
    return _dedupe_place_decisions(decisions)


def _entity_places(alignment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entity_support = alignment.get("entity_support") if isinstance(alignment.get("entity_support"), dict) else {}
    places = entity_support.get("places") if isinstance(entity_support.get("places"), list) else []
    return {_normalize_key(row.get("value")): row for row in places if isinstance(row, dict) and row.get("value")}


def _place_decision_evidence(entity: dict[str, Any], place_role: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    for match in entity.get("transcript_matches") or []:
        if isinstance(match, dict):
            evidence.append({**match, "source": "transcript_match"})
    for text in entity.get("evidence_matches") or []:
        evidence.append({"source": "model_evidence", "text": text})
    for text in place_role.get("evidence_texts") or []:
        evidence.append({"source": "place_role", "text": text})
    return evidence[:6]


def _dedupe_place_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {"selected": 3, "rejected_as_filming_location": 2, "candidate": 1}
    by_label: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        label = str(decision.get("label") or "").strip()
        if not label:
            continue
        key = _normalize_key(label)
        current = by_label.get(key)
        if not current or priority.get(str(decision.get("decision")), 0) > priority.get(str(current.get("decision")), 0):
            by_label[key] = decision
    return sorted(
        by_label.values(),
        key=lambda row: (
            -priority.get(str(row.get("decision")), 0),
            -(float(row.get("confidence") or 0.0)),
            str(row.get("label") or "").casefold(),
        ),
    )


def _should_retitle(
    timing_status: str,
    context_anchors: list[dict[str, Any]],
    rejected_places: list[dict[str, Any]],
    *,
    original_title: str,
) -> bool:
    if not context_anchors:
        return False
    if timing_status == "possible_misaligned":
        return True
    title_key = _normalize_key(original_title)
    return any(_normalize_key(place.get("label")) in title_key for place in rejected_places)


def _title_from_context(context_anchors: list[dict[str, Any]], *, event_type: str) -> str:
    strong_labels = [str(anchor.get("label")) for anchor in context_anchors if anchor.get("role") in STRONG_CONTEXT_ANCHOR_ROLES]
    generic_labels = [str(anchor.get("label")) for anchor in context_anchors if anchor.get("role") in GENERIC_CONTEXT_ANCHOR_ROLES]
    place = _preferred_place_label(strong_labels)
    activity = _activity_from_context(generic_labels, event_type=event_type)
    if place and activity:
        return f"{place} {activity}"
    if place:
        return f"{place} Visit"
    if activity:
        return activity
    return "Reconciled Event"


def _preferred_place_label(labels: list[str]) -> str:
    cleaned = [label for label in labels if label and not _is_generic_label(label)]
    if cleaned:
        return _title_case(cleaned[0])
    return ""


def _activity_from_context(labels: list[str], *, event_type: str) -> str:
    normalized = " ".join(_normalize_key(label) for label in labels)
    if "farmhouse" in normalized or "farm area" in normalized or "farm fields" in normalized:
        return "Farmhouse Visit"
    if "school" in normalized or event_type == "school":
        return "School Event"
    if "park" in normalized or "lake" in normalized:
        return "Park Outing"
    if "home" in normalized or event_type == "home":
        return "Home Activities"
    if event_type == "birthday":
        return "Birthday Celebration"
    if event_type == "travel":
        return "Visit"
    return "Event"


def _reconciled_confidence(
    context_anchors: list[dict[str, Any]],
    rejected_places: list[dict[str, Any]],
    alignment: dict[str, Any],
) -> float:
    anchor_score = max([float(anchor.get("confidence") or 0.0) for anchor in context_anchors] or [0.0])
    rejected_bonus = 0.08 if rejected_places else 0.0
    drift_bonus = 0.06 if alignment.get("suggested_source_ranges") else 0.0
    return min(0.95, anchor_score + rejected_bonus + drift_bonus)


def _reconciled_summary(
    original_summary: str,
    *,
    title_status: str,
    context_anchors: list[dict[str, Any]],
    rejected_places: list[dict[str, Any]],
) -> str:
    if title_status != "retitled_from_local_context":
        return original_summary
    selected = ", ".join(_unique_items(anchor.get("label") for anchor in context_anchors)[:3])
    rejected = ", ".join(_unique_items(place.get("label") for place in rejected_places)[:3])
    parts = []
    if selected:
        parts.append(f"Appears to be filmed around {selected} based on local transcript context.")
    if rejected:
        parts.append(f"{rejected} appears to be mentioned or off-window context, not the selected filming location.")
    if original_summary:
        parts.append(f"Original model summary: {original_summary}")
    return " ".join(parts)


def _reconciliation_warnings(
    *,
    timing_status: str,
    title_status: str,
    rejected_places: list[dict[str, Any]],
    alignment: dict[str, Any],
) -> list[str]:
    warnings = []
    if title_status == "retitled_from_local_context":
        warnings.append("Model title replaced by local transcript context.")
    if timing_status == "possible_misaligned":
        warnings.append("Model evidence appears stronger outside the claimed event range.")
    if rejected_places:
        warnings.append("Some model place claims are treated as mentioned/ambiguous rather than filming location.")
    if alignment.get("evidence_claim_statuses", {}).get("unmatched"):
        warnings.append("Some evidence claims could not be matched to local transcript.")
    return warnings


def _reconciliation_signals(
    context_anchors: list[dict[str, Any]],
    rejected_places: list[dict[str, Any]],
    alignment: dict[str, Any],
) -> list[str]:
    signals = []
    for anchor in context_anchors[:4]:
        label = str(anchor.get("label") or "")
        role = str(anchor.get("role") or "")
        distance = anchor.get("distance_to_event_s")
        if label:
            suffix = f", {int(round(float(distance)))}s from event" if isinstance(distance, (int, float)) else ""
            signals.append(f"{label} from {role}{suffix}")
    for place in rejected_places[:4]:
        signals.append(f"{place.get('label')} treated as {place.get('role') or place.get('reason')}")
    for claim_status, count in _count_values(
        claim.get("status") for claim in alignment.get("evidence_claims") or [] if isinstance(claim, dict)
    ).items():
        signals.append(f"{count} evidence claim(s) {claim_status}")
    return signals


def _event_source_video_ids(event: dict[str, Any], alignment: dict[str, Any]) -> list[str]:
    values = alignment.get("source_video_ids") if isinstance(alignment.get("source_video_ids"), list) else []
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if not values:
        values = metadata.get("source_video_ids") if isinstance(metadata.get("source_video_ids"), list) else []
    return _unique_items(values)


def _event_source_ranges(event: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    ranges = metadata.get("source_ranges") if isinstance(metadata.get("source_ranges"), list) else []
    return [row for row in ranges if isinstance(row, dict)]


def _metadata_list(event: dict[str, Any], key: str) -> list[str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    value = metadata.get(key) or event.get(key)
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _unique_items(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _normalize_key(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _title_case(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in text.split())


def _is_generic_label(value: str) -> bool:
    key = _normalize_key(value)
    return key in {"farmhouse farm area", "farm fields", "home", "park", "lake", "school", "room"}

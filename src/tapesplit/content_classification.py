from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


FAMILY_TERMS = {
    "baby",
    "birthday",
    "brother",
    "camcorder",
    "child",
    "children",
    "dad",
    "family",
    "father",
    "filip",
    "filya",
    "friend",
    "grandma",
    "grandpa",
    "home",
    "kid",
    "kids",
    "lena",
    "mom",
    "mother",
    "party",
    "philip",
    "school",
    "sister",
}

UNRELATED_TERMS = {
    "actor",
    "actors",
    "broadcast",
    "cartoon",
    "cast",
    "cinema",
    "copyright",
    "credits",
    "director",
    "dvd menu",
    "episode",
    "film",
    "movie",
    "producer",
    "production",
    "program",
    "studio",
    "television",
    "trailer",
    "tv",
}

GENERIC_PERSON_TERMS = {
    "adult",
    "adults",
    "children",
    "family",
    "friends",
    "group",
    "kids",
    "people",
}


def build_content_classifications_for_project(
    project_dir: Path,
    *,
    confidence_threshold: float = 0.62,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if confidence_threshold < 0 or confidence_threshold > 1:
        raise ValueError("confidence_threshold must be between 0 and 1")
    events = read_jsonl(project / "canonical_events.jsonl") or (
        read_jsonl(project / "events.jsonl") + read_jsonl(project / "gemini_events.jsonl")
    )
    if not events:
        raise FileNotFoundError("no events found; run `tapesplit rebuild` or `tapesplit stitch-events` first")

    visual_text_by_subject = _rows_by_subject(read_jsonl(project / "visual_text_observations.jsonl"))
    captions_by_subject = _rows_by_subject(read_jsonl(project / "visual_captions.jsonl"))
    faces_by_subject = _rows_by_subject(read_jsonl(project / "face_observations.jsonl"))
    evidence_by_event = _evidence_by_event(project)

    output_path = project / "content_classifications.jsonl"
    if output_path.exists():
        output_path.unlink()

    rows = []
    for index, event in enumerate(events, start=1):
        event_id = str(event.get("id") or "")
        classification = classify_event_content(
            event,
            visual_text=visual_text_by_subject.get(event_id, []),
            captions=captions_by_subject.get(event_id, []),
            faces=faces_by_subject.get(event_id, []),
            evidence=evidence_by_event.get(event_id, []),
            confidence_threshold=confidence_threshold,
        )
        row = {
            "id": f"content_classification_{index:06d}",
            "source_subject_type": "event",
            "source_subject_id": event_id,
            "source_video_id": _event_source_video_id(event),
            "start_s": event.get("start_s"),
            "end_s": event.get("end_s"),
            "title": event.get("title") or "",
            "method": "local_multisignal_content_classifier",
            "review_status": "needs_review" if classification["confidence"] < 0.82 else "unreviewed",
            **classification,
        }
        rows.append(row)
        append_jsonl(output_path, row)

    return {
        "project": str(project),
        "output": str(output_path),
        "content_classifications": len(rows),
        "by_label": dict(Counter(row["label"] for row in rows)),
        "confidence_threshold": confidence_threshold,
    }


def classify_event_content(
    event: dict[str, Any],
    *,
    visual_text: list[dict[str, Any]],
    captions: list[dict[str, Any]],
    faces: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    confidence_threshold: float = 0.62,
) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    relatedness = str(event.get("relatedness") or metadata.get("relatedness") or "").casefold()
    text_parts = [
        event.get("title"),
        event.get("summary"),
        metadata.get("title"),
        metadata.get("summary"),
        metadata.get("event_type"),
        " ".join(str(item) for item in metadata.get("people") or []),
        " ".join(str(item) for item in metadata.get("place_candidates") or []),
        " ".join(str(item) for item in metadata.get("date_candidates") or []),
        " ".join(str(row.get("text") or "") for row in visual_text),
        " ".join(str(row.get("caption") or "") for row in captions),
        " ".join(str(row.get("text") or "") for row in evidence[:8]),
    ]
    text = _normalize_text(" ".join(str(part or "") for part in text_parts))
    terms = set(text.split())
    phrase_text = f" {text} "

    family_hits = sorted(term for term in FAMILY_TERMS if _term_in_text(term, terms, phrase_text))
    unrelated_hits = sorted(term for term in UNRELATED_TERMS if _term_in_text(term, terms, phrase_text))
    people = [str(item) for item in metadata.get("people") or [] if str(item).strip()]
    named_people = [
        person
        for person in people
        if _normalize_text(person) and _normalize_text(person) not in GENERIC_PERSON_TERMS
    ]
    usable_face_count = sum(1 for face in faces if str(face.get("face_quality_status") or "") == "usable")
    total_face_count = len(faces)

    family_score = 0.0
    unrelated_score = 0.0
    reasons = []
    signals: dict[str, Any] = {
        "relatedness": relatedness,
        "family_terms": family_hits[:12],
        "unrelated_terms": unrelated_hits[:12],
        "named_people": named_people[:12],
        "usable_face_count": usable_face_count,
        "face_count": total_face_count,
        "ocr_text_count": len(visual_text),
        "caption_count": len(captions),
        "evidence_count": len(evidence),
    }

    if relatedness in {"non_content"}:
        return _classification(
            "non_content",
            0.96,
            ["event already marked as non-content"],
            signals,
        )
    if relatedness in {"likely_unrelated", "unrelated"}:
        unrelated_score += 0.7
        reasons.append("event already marked likely unrelated")
    if relatedness in {"likely_family", "family", "personal"}:
        family_score += 0.5
        reasons.append("event already marked likely family")

    family_score += min(len(family_hits) * 0.08, 0.32)
    unrelated_score += min(len(unrelated_hits) * 0.12, 0.48)
    if named_people:
        family_score += min(len(named_people) * 0.05, 0.2)
        reasons.append("named people are present")
    if usable_face_count:
        family_score += min(usable_face_count * 0.04, 0.16)
        reasons.append("usable face crops are present")
    elif total_face_count:
        family_score += 0.04
        reasons.append("face crops are present")
    if unrelated_hits:
        reasons.append("movie/TV/credits terms are present")
    if visual_text and unrelated_hits:
        unrelated_score += 0.08
        reasons.append("OCR supports possible non-family media")

    if unrelated_score >= family_score + 0.22 and unrelated_score >= confidence_threshold:
        label = "likely_unrelated"
        confidence = min(0.95, 0.5 + unrelated_score - family_score * 0.35)
    elif family_score >= unrelated_score + 0.12 and family_score >= confidence_threshold:
        label = "likely_family"
        confidence = min(0.93, 0.48 + family_score - unrelated_score * 0.25)
    else:
        label = "uncertain"
        confidence = max(0.35, min(0.68, 0.45 + abs(family_score - unrelated_score)))
        if not reasons:
            reasons.append("insufficient direct family or unrelated-content signal")

    return _classification(label, confidence, reasons, signals)


def _classification(label: str, confidence: float, reasons: list[str], signals: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": label,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "reasons": _unique_items(reasons),
        "signals": signals,
    }


def _evidence_by_event(project: Path) -> dict[str, list[dict[str, Any]]]:
    evidence_rows = [
        row
        for row in read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
        if row.get("kind") != "content_classification"
    ]
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events = read_jsonl(project / "canonical_events.jsonl") or read_jsonl(project / "gemini_events.jsonl")
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        for evidence_id in event.get("evidence_ids") or []:
            evidence = evidence_by_id.get(str(evidence_id))
            if evidence:
                grouped[event_id].append(evidence)
    return dict(grouped)


def _rows_by_subject(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        subject_id = str(row.get("source_subject_id") or "")
        if subject_id:
            grouped[subject_id].append(row)
    return dict(grouped)


def _event_source_video_id(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_video_id = event.get("source_video_id") or metadata.get("source_video_id")
    if source_video_id:
        return str(source_video_id)
    source_ranges = event.get("source_ranges") or metadata.get("source_ranges")
    if isinstance(source_ranges, list) and source_ranges and isinstance(source_ranges[0], dict):
        return str(source_ranges[0].get("source_video_id") or "")
    return ""


def _term_in_text(term: str, tokens: set[str], phrase_text: str) -> bool:
    normalized = _normalize_text(term)
    if " " in normalized:
        return f" {normalized} " in phrase_text
    return normalized in tokens


def _normalize_text(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _unique_items(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "")
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result

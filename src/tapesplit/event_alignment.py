from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re
from typing import Any

from tapesplit.grouping import (
    PERSON_NAME_EQUIVALENTS,
    _normalize_key as _entity_normalize_key,
    _person_spelling_key,
    _transliterate_cyrillic,
)
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


DEFAULT_ALIGNMENT_CONTEXT_SECONDS = 45.0
MAX_TRANSCRIPT_SUPPORT = 6
TRANSCRIPT_CONTEXT_ANCHOR_SECONDS = 180.0

STOPWORDS = {
    "about",
    "after",
    "and",
    "are",
    "around",
    "at",
    "for",
    "from",
    "in",
    "into",
    "is",
    "kak",
    "nam",
    "near",
    "net",
    "of",
    "on",
    "sejchas",
    "seychas",
    "the",
    "then",
    "this",
    "tak",
    "tam",
    "tebe",
    "to",
    "tut",
    "vot",
    "with",
    "вот",
    "для",
    "как",
    "ли",
    "мы",
    "на",
    "нам",
    "не",
    "нет",
    "по",
    "сейчас",
    "так",
    "там",
    "тебе",
    "тут",
    "это",
}

GENERIC_EVENT_TERMS = {
    "activity",
    "activities",
    "celebration",
    "children",
    "family",
    "home",
    "people",
    "playing",
    "trip",
    "video",
}

WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def build_event_alignments(
    project_dir: Path,
    *,
    context_seconds: float = DEFAULT_ALIGNMENT_CONTEXT_SECONDS,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}
    visibility = build_visibility_filter(project)
    events = [
        row
        for row in read_jsonl(project / "canonical_events.jsonl")
        if visibility.visible_row(row, evidence_by_id=evidence_by_id)
    ]
    transcripts_by_source = _transcripts_by_source(project, visibility)
    alias_index = _project_alias_index(project)
    place_roles_by_event_label = _place_roles_by_event_label(project)

    output = project / "event_alignments.jsonl"
    if output.exists():
        output.unlink()

    rows = []
    for index, event in enumerate(events, start=1):
        row = align_event(
            event,
            evidence_by_id=evidence_by_id,
            transcripts_by_source=transcripts_by_source,
            alias_index=alias_index,
            place_roles_by_event_label=place_roles_by_event_label,
            context_seconds=context_seconds,
            index=index,
        )
        rows.append(row)
        append_jsonl(output, row)

    by_status = Counter(str(row.get("timing_status") or "unknown") for row in rows)
    return {
        "project": str(project),
        "output": str(output),
        "event_alignments": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "context_seconds": context_seconds,
    }


def align_event(
    event: dict[str, Any],
    *,
    evidence_by_id: dict[str, dict[str, Any]],
    transcripts_by_source: dict[str, list[dict[str, Any]]],
    alias_index: dict[str, Any] | None = None,
    place_roles_by_event_label: dict[tuple[str, str], dict[str, Any]] | None = None,
    context_seconds: float,
    index: int = 1,
) -> dict[str, Any]:
    event_id = str(event.get("id") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_ranges = _event_source_ranges(event, evidence_by_id=evidence_by_id)
    cited_evidence = [evidence_by_id[evidence_id] for evidence_id in event.get("evidence_ids") or [] if evidence_id in evidence_by_id]
    evidence_texts = _evidence_texts(cited_evidence)

    people = _string_list(metadata.get("people"))
    places = _string_list(metadata.get("place_candidates"))
    dates = _string_list(metadata.get("date_candidates"))
    entity_values = [*people, *places, *dates]
    entity_aliases = _event_entity_aliases(people=people, places=places, dates=dates, alias_index=alias_index or {})
    terms = _event_terms(event, entity_values, evidence_texts)
    quote_texts = _quote_evidence_texts(cited_evidence)

    nearby_segments = _segments_near_ranges(transcripts_by_source, source_ranges, context_seconds=context_seconds)
    nearby_support = _rank_transcript_support(
        nearby_segments,
        terms=terms,
        entity_values=entity_values,
        entity_aliases=entity_aliases,
        quote_texts=quote_texts,
    )
    source_segments = _source_segments(transcripts_by_source, source_ranges)
    evidence_claims = _verify_evidence_claims(
        quote_texts,
        source_segments=source_segments,
        nearby_segments=nearby_segments,
        source_ranges=source_ranges,
        context_seconds=context_seconds,
    )
    global_support = _rank_transcript_support(
        source_segments,
        terms=terms,
        entity_values=entity_values,
        entity_aliases=entity_aliases,
        quote_texts=quote_texts,
    )
    outside_support = _outside_support(global_support, nearby_support, source_ranges, context_seconds=context_seconds)

    entity_support = {
        "people": _entity_support(
            people,
            nearby_segments,
            evidence_texts,
            entity_aliases,
            entity_kind="person",
            event_id=event_id,
            place_roles_by_event_label=place_roles_by_event_label or {},
        ),
        "places": _entity_support(
            places,
            nearby_segments,
            evidence_texts,
            entity_aliases,
            entity_kind="place",
            event_id=event_id,
            place_roles_by_event_label=place_roles_by_event_label or {},
        ),
        "dates": _entity_support(
            dates,
            nearby_segments,
            evidence_texts,
            entity_aliases,
            entity_kind="date",
            event_id=event_id,
            place_roles_by_event_label=place_roles_by_event_label or {},
        ),
    }
    entity_score = _entity_support_score(entity_support)
    nearby_score = max([float(row.get("score") or 0.0) for row in nearby_support] or [0.0])
    outside_score = max([float(row.get("score") or 0.0) for row in outside_support] or [0.0])
    evidence_score = 1.0 if evidence_texts else 0.0
    support_score = round(min(1.0, nearby_score * 0.55 + entity_score * 0.25 + evidence_score * 0.2), 3)
    timing_status, warnings = _alignment_status(
        source_ranges=source_ranges,
        nearby_score=nearby_score,
        outside_score=outside_score,
        entity_score=entity_score,
        evidence_score=evidence_score,
    )

    return {
        "id": f"event_alignment_{index:06d}",
        "canonical_event_id": event_id,
        "event_title": str(event.get("title") or ""),
        "timing_status": timing_status,
        "support_score": support_score,
        "source_video_ids": _unique_items(item["source_video_id"] for item in source_ranges),
        "source_ranges": source_ranges,
        "suggested_source_ranges": _suggested_source_ranges(outside_support, nearby_score=nearby_score),
        "transcript_context_anchors": _transcript_context_anchors(source_segments, source_ranges),
        "evidence_claims": evidence_claims,
        "transcript_support": nearby_support[:MAX_TRANSCRIPT_SUPPORT],
        "alternate_transcript_anchors": outside_support[:MAX_TRANSCRIPT_SUPPORT],
        "entity_support": entity_support,
        "evidence_support": [
            {
                "evidence_id": str(row.get("id") or ""),
                "kind": row.get("kind"),
                "source_video_id": row.get("source_video_id"),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
                "confidence": row.get("confidence"),
                "text": _shorten(row.get("text"), 260),
            }
            for row in cited_evidence[:8]
        ],
        "warnings": warnings,
        "signals": _alignment_signals(
            nearby_score=nearby_score,
            outside_score=outside_score,
            entity_score=entity_score,
            evidence_score=evidence_score,
        ),
        "suggested_review_status": "needs_review"
        if timing_status in {"missing_range", "model_only", "possible_misaligned", "unsupported"}
        else "unreviewed",
    }


def _transcripts_by_source(project: Path, visibility: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(project / "transcript_segments.jsonl"):
        if visibility.excluded_row(row):
            continue
        source_video_id = str(row.get("source_video_id") or "")
        if not source_video_id:
            continue
        grouped[source_video_id].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (_number_or_none(row.get("start_s")) or 0.0, _number_or_none(row.get("end_s")) or 0.0))
    return dict(grouped)


def _project_alias_index(project: Path) -> dict[str, Any]:
    people: dict[str, set[str]] = defaultdict(set)
    for group in read_jsonl(project / "people_groups.jsonl"):
        metadata = group.get("metadata") if isinstance(group.get("metadata"), dict) else {}
        labels = _unique_items(
            [
                group.get("label"),
                metadata.get("normalized_key"),
                *(group.get("aliases") or []),
                *(group.get("normalized_names") or []),
            ]
        )
        if not labels:
            continue
        variants = _person_alias_variants(labels)
        for label in labels:
            for key in _person_lookup_keys(label):
                people[key].update(variants)
    return {"people": {key: set(values) for key, values in people.items()}}


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


def _event_entity_aliases(
    *,
    people: list[str],
    places: list[str],
    dates: list[str],
    alias_index: dict[str, Any],
) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    people_index = alias_index.get("people") if isinstance(alias_index.get("people"), dict) else {}
    for person in people:
        variants = _person_alias_variants([person])
        for key in _person_lookup_keys(person):
            variants.update(people_index.get(key, set()))
        aliases[person] = variants
    for place in places:
        aliases[place] = _place_alias_variants(place)
    for date in dates:
        aliases[date] = _date_alias_variants(date)
    return aliases


def _person_lookup_keys(value: Any) -> set[str]:
    normalized = _normalize_key(value)
    keys = {normalized, _person_spelling_key(normalized)}
    for token in normalized.split():
        keys.add(token)
        keys.add(_person_spelling_key(token))
    return {key for key in keys if key}


def _person_alias_variants(labels: list[str]) -> set[str]:
    variants: set[str] = set()
    for label in labels:
        normalized = _normalize_key(label)
        if not normalized:
            continue
        variants.add(normalized)
        variants.add(_transliterate_cyrillic(normalized))
        spelling_key = _person_spelling_key(normalized)
        if spelling_key:
            variants.add(spelling_key)
        for token in normalized.split():
            variants.add(token)
            variants.add(_transliterate_cyrillic(token))
            token_key = _person_spelling_key(token)
            if token_key:
                variants.add(token_key)
                variants.update(PERSON_NAME_EQUIVALENTS.get(token_key, set()))
        variants.update(PERSON_NAME_EQUIVALENTS.get(spelling_key, set()))
    return {variant for variant in variants if variant}


def _place_alias_variants(value: str) -> set[str]:
    normalized = _normalize_key(value)
    variants = {normalized}
    if "," in str(value):
        variants.add(_normalize_key(str(value).split(",", 1)[0]))
    return {variant for variant in variants if variant}


def _date_alias_variants(value: str) -> set[str]:
    normalized = _normalize_key(value)
    tokens = normalized.split()
    variants = {normalized}
    variants.update(token for token in tokens if token.isdigit() and len(token) >= 4)
    return {variant for variant in variants if variant}


def _event_source_ranges(
    event: dict[str, Any],
    *,
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    ranges = []
    for source in [event.get("source_ranges"), metadata.get("source_ranges")]:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            interval = _interval(
                item.get("source_video_id"),
                item.get("start_s"),
                item.get("end_s"),
            )
            if interval:
                ranges.append(interval)

    if not ranges:
        for evidence_id in event.get("evidence_ids") or []:
            row = evidence_by_id.get(str(evidence_id))
            if not row:
                continue
            interval = _interval(row.get("source_video_id"), row.get("start_s"), row.get("end_s"))
            if interval:
                ranges.append(interval)
    return _merge_ranges(ranges)


def _segments_near_ranges(
    transcripts_by_source: dict[str, list[dict[str, Any]]],
    source_ranges: list[dict[str, Any]],
    *,
    context_seconds: float,
) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for source_range in source_ranges:
        source_video_id = source_range["source_video_id"]
        window_start = max(0.0, source_range["start_s"] - context_seconds)
        window_end = source_range["end_s"] + context_seconds
        for segment in transcripts_by_source.get(source_video_id, []):
            if not _overlaps(segment, window_start, window_end):
                continue
            key = str(segment.get("id") or f"{source_video_id}:{segment.get('start_s')}:{segment.get('end_s')}")
            if key in seen:
                continue
            seen.add(key)
            result.append(segment)
    return sorted(result, key=lambda row: (str(row.get("source_video_id") or ""), _number_or_none(row.get("start_s")) or 0.0))


def _source_segments(
    transcripts_by_source: dict[str, list[dict[str, Any]]],
    source_ranges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_ids = {str(row["source_video_id"]) for row in source_ranges if row.get("source_video_id")}
    rows = []
    for source_video_id in sorted(source_ids):
        rows.extend(transcripts_by_source.get(source_video_id, []))
    return rows


def _rank_transcript_support(
    segments: list[dict[str, Any]],
    *,
    terms: set[str],
    entity_values: list[str],
    entity_aliases: dict[str, set[str]],
    quote_texts: list[str],
) -> list[dict[str, Any]]:
    scored = []
    for segment in segments:
        score, matched_terms, matched_entities, matched_quotes = _segment_score(
            segment,
            terms=terms,
            entity_values=entity_values,
            entity_aliases=entity_aliases,
            quote_texts=quote_texts,
        )
        if score <= 0:
            continue
        scored.append(
            {
                "transcript_id": str(segment.get("id") or ""),
                "source_video_id": segment.get("source_video_id"),
                "start_s": segment.get("start_s"),
                "end_s": segment.get("end_s"),
                "score": round(score, 3),
                "matched_terms": matched_terms[:12],
                "matched_entities": matched_entities[:12],
                "matched_quotes": matched_quotes[:4],
                "text": _shorten(segment.get("text"), 260),
            }
        )
    return sorted(scored, key=lambda row: (-float(row["score"]), str(row.get("source_video_id") or ""), _number_or_none(row.get("start_s")) or 0.0))


def _outside_support(
    global_support: list[dict[str, Any]],
    nearby_support: list[dict[str, Any]],
    source_ranges: list[dict[str, Any]],
    *,
    context_seconds: float,
) -> list[dict[str, Any]]:
    nearby_ids = {row.get("transcript_id") for row in nearby_support if row.get("transcript_id")}
    outside = []
    for row in global_support:
        if row.get("transcript_id") in nearby_ids:
            continue
        distance = _distance_to_ranges(row, source_ranges)
        if distance is not None and distance <= context_seconds:
            continue
        outside.append({**row, "distance_to_event_s": round(distance, 3) if distance is not None else None})
    return outside


def _segment_score(
    segment: dict[str, Any],
    *,
    terms: set[str],
    entity_values: list[str],
    entity_aliases: dict[str, set[str]],
    quote_texts: list[str],
) -> tuple[float, list[str], list[str], list[str]]:
    text = str(segment.get("text") or "")
    text_tokens = set(_tokens(text))
    matched_terms = sorted(terms & text_tokens)
    term_denominator = max(4, min(len(terms), 14))
    term_score = min(len(matched_terms) / term_denominator, 1.0) if terms else 0.0
    matched_entities = [value for value in entity_values if _value_supported_by_text(value, text, entity_aliases=entity_aliases)]
    entity_score = min(len(matched_entities) * 0.18, 0.72)
    quote_score, matched_quotes = _quote_match_score(text, quote_texts)
    score = min(max(quote_score, quote_score * 0.72 + entity_score * 0.22, term_score * 0.65 + entity_score * 0.42), 1.0)
    return score, matched_terms, matched_entities, matched_quotes


def _entity_support(
    values: list[str],
    transcript_segments: list[dict[str, Any]],
    evidence_texts: list[str],
    entity_aliases: dict[str, set[str]],
    *,
    entity_kind: str,
    event_id: str,
    place_roles_by_event_label: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    transcript_texts = [str(row.get("text") or "") for row in transcript_segments]
    result = []
    for value in values:
        place_role = place_roles_by_event_label.get((event_id, _normalize_key(value))) if entity_kind == "place" else None
        transcript_matches = [
            _segment_ref(segment)
            for segment in transcript_segments
            if _value_supported_by_text(value, str(segment.get("text") or ""), entity_aliases=entity_aliases)
        ][:4]
        evidence_matches = [
            _shorten(text, 160)
            for text in evidence_texts
            if _value_supported_by_text(value, text, entity_aliases=entity_aliases)
        ][:4]
        if transcript_matches:
            status = "direct_transcript"
        elif evidence_matches:
            status = "model_evidence"
        elif place_role and not place_role.get("include_in_place_groups"):
            status = str(place_role.get("role") or "non_filming_context")
        elif _weak_token_support(value, " ".join(transcript_texts), entity_aliases=entity_aliases):
            status = "weak_transcript"
        else:
            status = "unsupported"
        support_row = {
            "value": value,
            "status": status,
            "transcript_matches": transcript_matches,
            "evidence_matches": evidence_matches,
        }
        if place_role:
            support_row["place_role"] = {
                "role": place_role.get("role"),
                "source_label": place_role.get("source_label"),
                "include_in_place_groups": place_role.get("include_in_place_groups"),
                "confidence": place_role.get("confidence"),
                "notes": place_role.get("notes") or [],
            }
        result.append(support_row)
    return result


def _entity_support_score(entity_support: dict[str, list[dict[str, Any]]]) -> float:
    statuses = [
        row.get("status")
        for rows in entity_support.values()
        for row in rows
        if row.get("value")
    ]
    if not statuses:
        return 0.0
    weights = {
        "direct_transcript": 1.0,
        "weak_transcript": 0.6,
        "model_evidence": 0.45,
        "generic_scene_type": 0.35,
        "ambiguous_place_reference": 0.12,
        "mentioned_destination": 0.12,
        "travel_plan": 0.12,
        "unsupported": 0.0,
    }
    return min(sum(weights.get(str(status), 0.0) for status in statuses) / len(statuses), 1.0)


def _alignment_status(
    *,
    source_ranges: list[dict[str, Any]],
    nearby_score: float,
    outside_score: float,
    entity_score: float,
    evidence_score: float,
) -> tuple[str, list[str]]:
    warnings = []
    if not source_ranges:
        warnings.append("event has no source-local time range")
        return "missing_range", warnings
    if outside_score >= max(0.45, nearby_score + 0.2):
        warnings.append("stronger transcript match appears outside event range")
        return "possible_misaligned", warnings
    if nearby_score >= 0.35 or entity_score >= 0.75:
        return "aligned", warnings
    if nearby_score >= 0.15 or entity_score >= 0.4:
        warnings.append("only partial transcript/entity support near event range")
        return "weakly_aligned", warnings
    if evidence_score > 0:
        warnings.append("only model/event evidence found near this event")
        return "model_only", warnings
    warnings.append("no transcript or evidence support found")
    return "unsupported", warnings


def _alignment_signals(
    *,
    nearby_score: float,
    outside_score: float,
    entity_score: float,
    evidence_score: float,
) -> list[str]:
    signals = []
    if nearby_score:
        signals.append(f"nearby transcript score {nearby_score:.2f}")
    if outside_score:
        signals.append(f"best outside transcript score {outside_score:.2f}")
    if entity_score:
        signals.append(f"entity support score {entity_score:.2f}")
    if evidence_score:
        signals.append("cited event evidence available")
    return signals


def _event_terms(event: dict[str, Any], entity_values: list[str], evidence_texts: list[str] | None = None) -> set[str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    fields = [
        event.get("title"),
        event.get("summary"),
        metadata.get("event_type"),
        " ".join(entity_values),
        " ".join(evidence_texts or []),
    ]
    terms = set()
    for field in fields:
        terms.update(_tokens(field))
    return {term for term in terms if term not in GENERIC_EVENT_TERMS}


def _evidence_texts(rows: list[dict[str, Any]]) -> list[str]:
    texts = []
    for row in rows:
        if row.get("text"):
            texts.append(str(row["text"]))
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        evidence_text = metadata.get("evidence_text")
        if isinstance(evidence_text, list):
            texts.extend(str(item) for item in evidence_text if item)
        elif evidence_text:
            texts.append(str(evidence_text))
    return texts


def _quote_evidence_texts(rows: list[dict[str, Any]]) -> list[str]:
    texts = []
    for row in rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        evidence_text = metadata.get("evidence_text")
        if isinstance(evidence_text, list):
            texts.extend(_clean_quote_text(item) for item in evidence_text if _clean_quote_text(item))
        elif evidence_text:
            cleaned = _clean_quote_text(evidence_text)
            if cleaned:
                texts.append(cleaned)
    return _unique_items(texts)


def _verify_evidence_claims(
    quote_texts: list[str],
    *,
    source_segments: list[dict[str, Any]],
    nearby_segments: list[dict[str, Any]],
    source_ranges: list[dict[str, Any]],
    context_seconds: float,
) -> list[dict[str, Any]]:
    nearby_ids = {_segment_id(segment) for segment in nearby_segments}
    rows = []
    for quote in quote_texts[:12]:
        scored = []
        for segment in source_segments:
            score, _ = _quote_match_score(str(segment.get("text") or ""), [quote])
            if score <= 0:
                continue
            scored.append((score, segment))
        scored.sort(key=lambda item: (-item[0], _number_or_none(item[1].get("start_s")) or 0.0))
        if not scored or scored[0][0] < 0.45:
            rows.append(
                {
                    "text": _shorten(quote, 180),
                    "status": "unmatched",
                    "best_match": None,
                    "score": 0.0,
                }
            )
            continue

        score, best = scored[0]
        distance = _distance_to_ranges(best, source_ranges)
        best_id = _segment_id(best)
        if best_id in nearby_ids or (distance is not None and distance <= context_seconds):
            status = "nearby_transcript"
        else:
            status = "relocated_transcript"
        rows.append(
            {
                "text": _shorten(quote, 180),
                "status": status,
                "score": round(score, 3),
                "distance_to_event_s": round(distance, 3) if distance is not None else None,
                "best_match": _segment_ref(best),
            }
        )
    return rows


def _quote_match_score(text: str, quote_texts: list[str]) -> tuple[float, list[str]]:
    target_tokens = set(_tokens(text))
    target_keys = _spelling_token_keys(text)
    best_score = 0.0
    matched = []
    for quote in quote_texts:
        quote_tokens = set(_tokens(quote))
        quote_keys = _spelling_token_keys(quote)
        denominator = max(2, min(len(quote_tokens) or len(quote_keys), 8))
        direct_overlap = quote_tokens & target_tokens
        spelling_overlap = quote_keys & target_keys
        score = min(max(len(direct_overlap), len(spelling_overlap)) / denominator, 1.0)
        if score >= 0.5 and (len(direct_overlap) >= 2 or len(spelling_overlap) >= 2 or score >= 1.0):
            matched.append(_shorten(quote, 140))
            best_score = max(best_score, score)
    return best_score, matched


def _suggested_source_ranges(outside_support: list[dict[str, Any]], *, nearby_score: float) -> list[dict[str, Any]]:
    if not outside_support:
        return []
    best_score = max(float(row.get("score") or 0.0) for row in outside_support)
    threshold = max(0.45, nearby_score + 0.2, best_score - 0.15)
    anchors = [row for row in outside_support if float(row.get("score") or 0.0) >= threshold]
    if not anchors:
        return []
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for anchor in anchors[:8]:
        source_video_id = str(anchor.get("source_video_id") or "")
        if source_video_id:
            by_source[source_video_id].append(anchor)
    ranges = []
    for source_video_id, rows in by_source.items():
        starts = [_number_or_none(row.get("start_s")) for row in rows]
        ends = [_number_or_none(row.get("end_s")) or _number_or_none(row.get("start_s")) for row in rows]
        starts = [value for value in starts if value is not None]
        ends = [value for value in ends if value is not None]
        if not starts or not ends:
            continue
        score = max(float(row.get("score") or 0.0) for row in rows)
        ranges.append(
            {
                "source_video_id": source_video_id,
                "start_s": round(max(0.0, min(starts) - 10.0), 3),
                "end_s": round(max(ends) + 10.0, 3),
                "confidence": round(min(0.95, score), 3),
                "basis": "stronger_transcript_anchor_outside_event_range",
                "transcript_ids": _unique_items(row.get("transcript_id") for row in rows),
            }
        )
    return sorted(ranges, key=lambda row: (-float(row["confidence"]), str(row["source_video_id"]), float(row["start_s"])))


def _transcript_context_anchors(
    source_segments: list[dict[str, Any]],
    source_ranges: list[dict[str, Any]],
    *,
    context_seconds: float = TRANSCRIPT_CONTEXT_ANCHOR_SECONDS,
) -> list[dict[str, Any]]:
    anchors = []
    seen = set()
    for segment in source_segments:
        distance = _distance_to_ranges(segment, source_ranges)
        if distance is None or distance > context_seconds:
            continue
        for anchor in _anchors_from_transcript_segment(segment):
            key = (anchor["label"].casefold(), anchor["role"], anchor["transcript_id"])
            if key in seen:
                continue
            seen.add(key)
            anchors.append({**anchor, "distance_to_event_s": round(distance, 3)})
    return sorted(anchors, key=lambda row: (float(row["distance_to_event_s"]), str(row["label"]).casefold()))[:12]


def _anchors_from_transcript_segment(segment: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(segment.get("text") or "")
    anchors = []
    for pattern in [
        r"\b(?:city|town|place)\s+(?:is\s+called|called|name\s+is)\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})",
        r"\b(?:we are|we're|here we are)\s+(?:in|at|near)\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})",
        r"\b(?:город|место)\s+называется\s+([A-ZА-ЯЁ][\w.'-]*(?:\s+[A-ZА-ЯЁ][\w.'-]*){0,3})",
    ]:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.UNICODE):
            label = _clean_anchor_label(match.group(1))
            if label:
                anchors.append(_anchor_record(segment, label, role="spoken_location_anchor", confidence=0.82))

    lowered = text.casefold()
    generic_patterns = [
        ("farmhouse / farm area", ["фермерский домик", "фермерский район", "farmhouse", "farm house", "farm area"]),
        ("farm fields", ["кукурузные поля", "corn fields", "cornfields"]),
    ]
    for label, needles in generic_patterns:
        if any(needle in lowered for needle in needles):
            anchors.append(_anchor_record(segment, label, role="generic_place_context", confidence=0.66))
    return anchors


def _anchor_record(segment: dict[str, Any], label: str, *, role: str, confidence: float) -> dict[str, Any]:
    return {
        "label": label,
        "role": role,
        "confidence": confidence,
        "transcript_id": _segment_id(segment),
        "source_video_id": segment.get("source_video_id"),
        "start_s": segment.get("start_s"),
        "end_s": segment.get("end_s"),
        "text": _shorten(segment.get("text"), 160),
    }


def _clean_anchor_label(value: str) -> str:
    label = re.sub(r"[^\w\s.'-]+$", "", str(value or "").strip(), flags=re.UNICODE)
    label = re.sub(r"\s+", " ", label).strip(" .'-")
    if len(label) < 3:
        return ""
    return label


def _value_supported_by_text(value: str, text: str, *, entity_aliases: dict[str, set[str]] | None = None) -> bool:
    aliases = _aliases_for_value(value, entity_aliases)
    target = _normalize_text(text)
    if not aliases or not target:
        return False
    if any(_phrase_supported(alias, target) for alias in aliases):
        return True
    target_tokens = set(_tokens(text))
    target_keys = _spelling_token_keys(text)
    for alias in aliases:
        alias_tokens = set(_tokens(alias))
        alias_keys = _spelling_token_keys(alias)
        if alias_tokens and alias_tokens <= target_tokens:
            return True
        if alias_keys and (alias_keys <= target_keys or _all_keys_have_fuzzy_support(alias_keys, target_keys)):
            return True
        if any(token.isdigit() and token in target_tokens for token in alias_tokens):
            return True
    return False


def _weak_token_support(value: str, text: str, *, entity_aliases: dict[str, set[str]] | None = None) -> bool:
    aliases = _aliases_for_value(value, entity_aliases)
    value_tokens = set(token for alias in aliases for token in _tokens(alias))
    value_keys = _spelling_token_keys(" ".join(aliases))
    if not value_tokens and not value_keys:
        return False
    target_tokens = set(_tokens(text))
    target_keys = _spelling_token_keys(text)
    return bool((value_tokens & target_tokens) or (value_keys & target_keys) or _any_key_has_fuzzy_support(value_keys, target_keys))


def _aliases_for_value(value: str, entity_aliases: dict[str, set[str]] | None) -> set[str]:
    aliases = {str(value or "").strip(), _normalize_key(value)}
    if entity_aliases:
        aliases.update(entity_aliases.get(value, set()))
    return {alias for alias in aliases if alias}


def _phrase_supported(alias: str, normalized_text: str) -> bool:
    normalized_alias = _normalize_text(alias)
    if len(normalized_alias) < 3:
        return False
    return bool(re.search(rf"(?<![\w]){re.escape(normalized_alias)}(?![\w])", normalized_text, flags=re.UNICODE))


def _spelling_token_keys(value: Any) -> set[str]:
    keys = set()
    for token in _entity_normalize_key(value).split():
        for key in _spelling_key_variants(token):
            if key and key not in STOPWORDS and len(key) >= 3:
                keys.add(key)
    return keys


def _spelling_key_variants(token: str) -> set[str]:
    variants = {_person_spelling_key(token)}
    for suffix in [
        "ami",
        "ами",
        "om",
        "em",
        "oj",
        "oy",
        "ej",
        "ey",
        "ой",
        "ом",
        "ем",
        "ей",
        "ым",
        "у",
        "а",
        "е",
        "ы",
        "и",
        "u",
        "a",
        "e",
        "y",
        "i",
        "o",
    ]:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            variants.add(_person_spelling_key(token[: -len(suffix)]))
    return {variant for variant in variants if variant}


def _all_keys_have_fuzzy_support(value_keys: set[str], target_keys: set[str]) -> bool:
    return bool(value_keys) and all(_key_has_fuzzy_support(key, target_keys) for key in value_keys)


def _any_key_has_fuzzy_support(value_keys: set[str], target_keys: set[str]) -> bool:
    return any(_key_has_fuzzy_support(key, target_keys) for key in value_keys)


def _key_has_fuzzy_support(key: str, target_keys: set[str]) -> bool:
    if len(key) < 4:
        return False
    return any(_loose_key_match(key, target) for target in target_keys if len(target) >= 4)


def _loose_key_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.startswith(right) or right.startswith(left):
        return min(len(left), len(right)) >= 4
    left_prefix = left[:4]
    right_prefix = right[:4]
    if left_prefix != right_prefix:
        return False
    return abs(len(left) - len(right)) <= 2


def _clean_quote_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.strip("\"'`“”‘’ ")


def _segment_id(segment: dict[str, Any]) -> str:
    return str(segment.get("id") or f"{segment.get('source_video_id')}:{segment.get('start_s')}:{segment.get('end_s')}")


def _segment_ref(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "transcript_id": str(segment.get("id") or ""),
        "source_video_id": segment.get("source_video_id"),
        "start_s": segment.get("start_s"),
        "end_s": segment.get("end_s"),
        "text": _shorten(segment.get("text"), 120),
    }


def _overlaps(segment: dict[str, Any], start_s: float, end_s: float) -> bool:
    segment_start = _number_or_none(segment.get("start_s"))
    segment_end = _number_or_none(segment.get("end_s"))
    if segment_start is None:
        return False
    if segment_end is None:
        segment_end = segment_start
    return segment_start <= end_s and segment_end >= start_s


def _distance_to_ranges(row: dict[str, Any], source_ranges: list[dict[str, Any]]) -> float | None:
    source_video_id = str(row.get("source_video_id") or "")
    start_s = _number_or_none(row.get("start_s"))
    end_s = _number_or_none(row.get("end_s"))
    if start_s is None:
        return None
    if end_s is None:
        end_s = start_s
    distances = []
    for source_range in source_ranges:
        if source_video_id and source_video_id != source_range["source_video_id"]:
            continue
        if end_s < source_range["start_s"]:
            distances.append(source_range["start_s"] - end_s)
        elif start_s > source_range["end_s"]:
            distances.append(start_s - source_range["end_s"])
        else:
            distances.append(0.0)
    return min(distances) if distances else None


def _merge_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = []
    for row in sorted(ranges, key=lambda item: (item["source_video_id"], item["start_s"], item["end_s"])):
        if not merged or row["source_video_id"] != merged[-1]["source_video_id"] or row["start_s"] > merged[-1]["end_s"] + 1.0:
            merged.append(dict(row))
            continue
        merged[-1]["end_s"] = max(merged[-1]["end_s"], row["end_s"])
    return merged


def _interval(source_video_id: Any, start_s: Any, end_s: Any) -> dict[str, Any] | None:
    source = str(source_video_id or "")
    start = _number_or_none(start_s)
    end = _number_or_none(end_s)
    if not source or start is None:
        return None
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    return {"source_video_id": source, "start_s": round(start, 3), "end_s": round(end, 3)}


def _tokens(value: Any) -> list[str]:
    text = _normalize_text(value)
    tokens = []
    for token in WORD_RE.findall(text):
        if len(token) < 3:
            continue
        if token in STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _normalize_key(value: Any) -> str:
    return _entity_normalize_key(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        result.append(text)
    return result


def _unique_items(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _shorten(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

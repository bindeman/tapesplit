"""People outside the family: friends, classmates, teachers and family friends.

tapesplit listens for the words people use about them (друг, подруга, воспитательница,
"friend", "teacher", a child's дядя/тётя for an adult), ties each one to a name said in the
same breath, and adds the context of every moment that person turns up in: the years, and
whether it was at home (in which residence era) or on a trip. Like every relationship, these
are candidates until someone confirms them.

The family itself comes from the kinship candidates: the child they point at, and everyone
named as a mother, father, grandparent or sibling. Those people are never someone's friend.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Any

FRIEND_TERMS = (
    "друг", "друга", "другу", "другом", "друзья", "друзей", "друзьям", "друзьями",
    "подруга", "подругу", "подруги", "подруге", "подругой", "подружка", "подружку", "подружки",
    "товарищ", "товарищи", "товарищей", "товарищам",
    "friend", "friends", "buddy", "buddies",
)
TEACHER_TERMS = (
    "учительница", "учительницу", "учительницы", "учительнице", "учитель", "учителя", "учителю",
    "воспитательница", "воспитательницу", "воспитательницы", "воспитательнице", "воспитатель", "воспитателя",
    "teacher", "teachers", "babysitter", "nanny",
)
OUR_TERMS = ("наш", "наша", "наши", "нашего", "нашей", "нашу", "нашим", "our")
MY_TERMS = ("мой", "моя", "мои", "моего", "моей", "мою", "my")
CLASSMATE_PATTERNS = (
    re.compile(r"ходит\s+с\s+\S+\s+в\s+(?:детский\s+)?сад", re.IGNORECASE),
    re.compile(r"в\s+одн(?:ом|у)\s+(?:класс|групп|садик)", re.IGNORECASE),
    re.compile(r"(?<![\w])classmates?(?![\w])", re.IGNORECASE),
)
# A child's дядя/тётя + a first name: an uncle or aunt, or (very often) a family friend.
HONORIFIC = re.compile(r"(?<![\w])(дяд[яеюи]|т[её]т[яеюи])\s+([А-ЯЁ][а-яё]{2,})")

_WORD = re.compile(r"\w+", re.UNICODE)


def _term_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![\w])", re.IGNORECASE)


FRIEND_RE = _term_pattern(FRIEND_TERMS)
TEACHER_RE = _term_pattern(TEACHER_TERMS)
OUR_RE = _term_pattern(OUR_TERMS)
MY_RE = _term_pattern(MY_TERMS)

BASE_CONFIDENCE = {"named_in_line": 0.72, "named_nearby": 0.62, "honorific": 0.55}
PREDICATE_QUESTIONS = {
    "friend_or_classmate_candidate": "Is {subject} {object}'s friend or classmate?",
    "family_friend_candidate": "Is {subject} a friend of the family?",
    "teacher_or_caretaker_candidate": "Is {subject} {object}'s teacher or caretaker?",
    "honorific_family_friend_candidate": "Is {subject} a relative or a family friend?",
}
LOOP_WINDOW_S = 120.0  # the same words again within two minutes are one line (Whisper loops)


def build_social_candidates(
    *,
    transcripts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    person_entities: list[dict[str, Any]],
    kinship_candidates: list[dict[str, Any]],
    date_groups: list[dict[str, Any]],
    era_contexts: list[dict[str, Any]],
    event_for_segment: Any,
    contains_term: Any,
    context_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Social relationship candidates, each scoped to the moments its person appears in.

    The callables come from relationships.py so both resolvers match terms and moments the same way.
    """
    child_id, family_ids = _family(kinship_candidates)
    child = next((entity for entity in person_entities if entity["id"] == child_id), None)
    others = [entity for entity in person_entities if entity["id"] not in family_ids]
    lines = _dedupe_loops(transcripts)
    for line in lines:
        line["_words"] = _words(str(line.get("text") or ""))
    event_dates = _event_dates(date_groups)

    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, line in enumerate(lines):
        text = str(line.get("text") or "")
        kinds = _social_kinds(text, contains_term)
        honorifics = [(m.group(1), m.group(2)) for m in HONORIFIC.finditer(text)]
        if not kinds and not honorifics:
            continue
        event = event_for_segment(events, line)
        if event is None:
            continue
        nearby = _nearby_text(lines, index, context_seconds)
        for kind in kinds:
            named = [e for e in others if _mentions(text, e, contains_term, line["_words"])]
            source = "named_in_line"
            if not named:
                named = [e for e in others if _mentions(nearby, e, contains_term)]
                source = "named_nearby"
            for entity in named:
                predicate, object_id, object_label = _relation(kind, text, nearby, event, child, contains_term)
                _add(buckets, predicate, entity, object_id, object_label, line, event, kind, source)
        for title, name in honorifics:
            entity = next((e for e in others if _mentions(name, e, contains_term)), None)
            if entity is None:
                if any(_mentions(name, e, contains_term) for e in person_entities if e["id"] in family_ids):
                    continue
                entity = {"id": f"person_name_{name.casefold()}", "label": name, "aliases": {name.casefold()}}
            _add(buckets, "honorific_family_friend_candidate", entity, "family", "the family", line, event,
                 title.casefold(), "honorific")

    family = [e for e in person_entities if e["id"] in family_ids]
    candidates = []
    for bucket in buckets.values():
        scope = _scope(bucket["entity"], events, lines, event_dates, era_contexts, family, event_for_segment,
                       contains_term)
        distinct = len(bucket["moments"])
        confidence = min(0.9, BASE_CONFIDENCE[bucket["source"]] + min(distinct - 1, 3) * 0.04)
        candidates.append({
            "subject_entity_id": bucket["entity"]["id"],
            "subject_label": bucket["entity"]["label"],
            "predicate": bucket["predicate"],
            "object_entity_id": bucket["object_id"],
            "object_label": bucket["object_label"],
            "direction": "subject_to_object",
            "scope": scope,
            "confidence": round(confidence, 2),
            "supporting_signals": _signals(bucket),
            "evidence_ids": bucket["evidence_ids"],
            "contradicting_evidence_ids": [],
            "source": "local_social_resolver",
            "review_status": "needs_review",
            "metadata": {
                "context_source": bucket["source"],
                "kinds": sorted(bucket["kinds"]),
                "transcript_segment_ids": bucket["segment_ids"],
                "source_texts": bucket["texts"][:5],
                "said_at": bucket["said_at"][:5],
            },
        })
    candidates.sort(key=lambda c: (c["scope"].get("first_date") or "9999", c["subject_label"]))
    return candidates


def social_review_question(candidate: dict[str, Any]) -> str:
    template = PREDICATE_QUESTIONS.get(str(candidate.get("predicate")), "How is {subject} related to {object}?")
    return template.format(subject=candidate.get("subject_label") or "this person",
                           object=candidate.get("object_label") or "the family")


# ---------------------------------------------------------------------------- the family


def _family(kinship_candidates: list[dict[str, Any]]) -> tuple[str | None, set[str]]:
    objects = Counter(str(c.get("object_entity_id")) for c in kinship_candidates if c.get("object_entity_id"))
    child_id = objects.most_common(1)[0][0] if objects else None
    family = {str(c.get(key)) for c in kinship_candidates for key in ("subject_entity_id", "object_entity_id") if c.get(key)}
    return child_id, family


# ---------------------------------------------------------------------------- reading lines


def _dedupe_loops(transcripts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per thing said: drop exact repeats on the same tape within LOOP_WINDOW_S."""
    last_seen: dict[tuple[str, str], float] = {}
    kept = []
    for line in transcripts:
        text = re.sub(r"\W+", " ", str(line.get("text") or "")).strip().casefold()
        start = _number(line.get("start_s"))
        key = (str(line.get("source_video_id") or ""), text)
        if not text or start is None:
            continue
        previous = last_seen.get(key)
        last_seen[key] = start
        if previous is not None and start - previous < LOOP_WINDOW_S:
            continue
        kept.append(dict(line))
    return kept


def _social_kinds(text: str, contains_term: Any) -> list[str]:
    kinds = []
    if FRIEND_RE.search(text):
        kinds.append("friend")
    if TEACHER_RE.search(text):
        kinds.append("teacher")
    if any(pattern.search(text) for pattern in CLASSMATE_PATTERNS):
        kinds.append("classmate")
    return kinds


def _words(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(text)}


def _mentions(text: str, entity: dict[str, Any], contains_term: Any, words: set[str] | None = None) -> bool:
    """Whole-word, case-insensitive: single-word aliases by set lookup, longer ones by regex."""
    words = _words(text) if words is None else words
    for alias in entity.get("aliases", ()):
        alias = str(alias).casefold()
        if len(alias) < 3:
            continue
        if " " in alias or "-" in alias:
            if contains_term(text, alias):
                return True
        elif alias in words:
            return True
    return False


def _nearby_text(lines: list[dict[str, Any]], index: int, seconds: float) -> str:
    center = _number(lines[index].get("start_s")) or 0.0
    video = lines[index].get("source_video_id")
    parts = []
    for line in lines[max(0, index - 12): index + 13]:
        start = _number(line.get("start_s"))
        if line.get("source_video_id") == video and start is not None and abs(start - center) <= seconds:
            parts.append(str(line.get("text") or ""))
    return " ".join(parts)


def _relation(kind: str, text: str, nearby: str, event: dict[str, Any], child: dict[str, Any] | None,
              contains_term: Any) -> tuple[str, str, str]:
    child_id = child["id"] if child else "child"
    child_label = child["label"] if child else "the child"
    ours = bool(OUR_RE.search(text))
    mine = bool(MY_RE.search(text))
    about_child = bool(child) and (_mentions(nearby, child, contains_term) or _event_has(event, child))
    if kind == "teacher":
        if mine and not ours:
            return "teacher_or_caretaker_candidate", "speaker", "the person filming"
        return "teacher_or_caretaker_candidate", child_id, child_label
    if kind == "classmate":
        return "friend_or_classmate_candidate", child_id, child_label
    if ours and not about_child:
        return "family_friend_candidate", "family", "the family"
    if about_child:
        return "friend_or_classmate_candidate", child_id, child_label
    return "family_friend_candidate", "family", "the family"


def _event_has(event: dict[str, Any], entity: dict[str, Any]) -> bool:
    people = (event.get("metadata") or {}).get("people") or []
    aliases = {str(a).casefold() for a in entity.get("aliases", ())}
    return any(str(person).casefold() in aliases for person in people)


def _add(buckets: dict, predicate: str, entity: dict[str, Any], object_id: str, object_label: str,
         line: dict[str, Any], event: dict[str, Any], kind: str, source: str) -> None:
    key = (predicate, entity["id"], object_id)
    bucket = buckets.setdefault(key, {
        "predicate": predicate, "entity": entity, "object_id": object_id, "object_label": object_label,
        "source": source, "kinds": set(), "evidence_ids": [], "segment_ids": [], "texts": [], "said_at": [],
        "moments": set(),
    })
    if BASE_CONFIDENCE[source] > BASE_CONFIDENCE[bucket["source"]]:
        bucket["source"] = source
    bucket["kinds"].add(kind)
    evidence_id = line.get("evidence_id") or line.get("id")
    if evidence_id and evidence_id not in bucket["evidence_ids"]:
        bucket["evidence_ids"].append(evidence_id)
    if line.get("id") and line.get("id") not in bucket["segment_ids"]:
        bucket["segment_ids"].append(line.get("id"))
    text = str(line.get("text") or "").strip()
    if text not in bucket["texts"]:
        bucket["texts"].append(text)
        bucket["said_at"].append({"source_video_id": line.get("source_video_id"), "start_s": _number(line.get("start_s"))})
    bucket["moments"].add(str(event.get("id")))


def _signals(bucket: dict[str, Any]) -> list[str]:
    signals = [f"said about {bucket['entity']['label']}: {', '.join(sorted(bucket['kinds']))}"]
    if bucket["source"] == "named_nearby":
        signals.append("the name is said within a few seconds, not in the same line")
    if bucket["source"] == "honorific":
        signals.append("a child's дядя/тётя: often a family friend, sometimes a relative")
    return signals


# ---------------------------------------------------------------------------- context


def _event_dates(date_groups: list[dict[str, Any]]) -> dict[str, str]:
    """The best recording date per moment: day precision first, then month, then year."""
    rank = {"day": 0, "month": 1, "year": 2}
    best: dict[str, tuple[int, str]] = {}
    for group in date_groups:
        value = group.get("date_value")
        precision = str(group.get("precision") or "")
        if not value or group.get("excluded_as_event_date") or precision not in rank:
            continue
        for event_id in group.get("canonical_event_ids") or []:
            candidate = (rank[precision], str(value))
            if event_id not in best or candidate < best[event_id]:
                best[event_id] = candidate
    return {event_id: value for event_id, (_, value) in best.items()}


def _era_for_year(year: int | None, era_contexts: list[dict[str, Any]]) -> str | None:
    if year is None:
        return None
    for era in era_contexts:
        start, end = era.get("start_year"), era.get("end_year")
        if start is not None and end is not None and int(start) <= year <= int(end):
            return str(era.get("residence_label") or era.get("label") or "")
    return None


def _scope(entity: dict[str, Any], events: list[dict[str, Any]], lines: list[dict[str, Any]],
           event_dates: dict[str, str], era_contexts: list[dict[str, Any]], family: list[dict[str, Any]],
           event_for_segment: Any, contains_term: Any) -> dict[str, Any]:
    """Every moment this person turns up in: named by the video model, or named on the soundtrack."""
    moments: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_has(event, entity):
            moments[str(event.get("id"))] = event
    for line in lines:
        if _mentions(str(line.get("text") or ""), entity, contains_term, line.get("_words")):
            event = event_for_segment(events, line)
            if event is not None:
                moments[str(event.get("id"))] = event

    dates, contexts, tapes, with_family = [], Counter(), set(), Counter()
    for event_id, event in moments.items():
        metadata = event.get("metadata") or {}
        date = event_dates.get(event_id)
        year = int(date[:4]) if date and date[:4].isdigit() else None
        if date:
            dates.append(date)
        if str(metadata.get("event_type") or "") == "travel":
            contexts["a trip"] += 1
        else:
            contexts[_era_for_year(year, era_contexts) or "at home, year unknown"] += 1
        for video_id in metadata.get("source_video_ids") or []:
            tapes.add(str(video_id))
        for member in family:
            if _event_has(event, member):
                with_family[member["label"]] += 1
    dates.sort()
    only_trips = bool(moments) and set(contexts) == {"a trip"}
    return {
        "moments": len(moments),
        "dated_moments": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "contexts": [{"label": label, "moments": count} for label, count in contexts.most_common()],
        "met_on_trip": only_trips and len(moments) == 1,
        "source_video_ids": sorted(tapes),
        "canonical_event_ids": sorted(moments),
        "with_family": [name for name, _ in with_family.most_common()],
    }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

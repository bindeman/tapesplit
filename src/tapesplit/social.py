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

from tapesplit.grouping import _transliterate_cyrillic

FRIEND_TERMS = (
    "друг", "друга", "другу", "другом", "друзья", "друзей", "друзьям", "друзьями",
    "подруга", "подругу", "подруги", "подруге", "подругой", "подружка", "подружку", "подружки",
    "товарищ", "товарищи", "товарищей", "товарищам",
    "friend", "friends", "buddy", "buddies",
)
TEACHER_TERMS = (
    "учительница", "учительницу", "учительницы", "учительнице", "учитель", "учителя", "учителю",
    "воспитательница", "воспитательницу", "воспитательницы", "воспитательнице", "воспитатель", "воспитателя",
    "директриса", "директрису", "директрисы", "директрисе",
    "teacher", "teachers", "principal", "babysitter", "nanny",
)
OUR_TERMS = ("наш", "наша", "наши", "нашего", "нашей", "нашу", "нашим", "our")
MY_TERMS = ("мой", "моя", "мои", "моего", "моей", "мою", "my")
CLASSMATE_PATTERNS = (
    re.compile(r"ходит\s+с\s+\S+\s+в\s+(?:детский\s+)?сад", re.IGNORECASE),
    re.compile(r"(?i:ходят)\s+в\s+одну\s+(?i:группу|школу)"),
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

# A capitalized Russian first name (and maybe a surname). Only the relationship word is
# case-insensitive; the name must really be capitalized, and Latin capitals at the
# start of English sentences ("Look, my friend") are not names.
_NAME = r"[А-ЯЁ][а-яё]{2,}"


def _word_then_name(terms: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])(?i:" + "|".join(re.escape(t) for t in terms) + r")\s+(" + _NAME + r")")


FRIEND_THEN_NAME = _word_then_name(FRIEND_TERMS)
TEACHER_THEN_NAME = _word_then_name(TEACHER_TERMS)
NAME_THEN_OUR_FRIEND = re.compile(
    r"(" + _NAME + r"(?:\s+" + _NAME + r")?),\s+(?i:наш\w*|мо[йяи]\w*)\s+(?i:друг\w*|подруг\w*)"
)
NEAR_WORDS = 5  # a name counts only this close to the relationship word, in the same sentence
SENTENCE_END = re.compile(r"[.!?…]")
SAME_MOMENT_S = 10.0  # two transcripts of the same few seconds

BASE_CONFIDENCE = {"named_in_line": 0.72, "named_next_to_word": 0.7, "named_nearby": 0.62, "honorific": 0.55}
PREDICATE_QUESTIONS = {
    "friend_or_classmate_candidate": "Is {subject} {object}'s friend or classmate?",
    "family_friend_candidate": "Is {subject} a friend of the family?",
    "teacher_or_caretaker_candidate": "Is {subject} {object}'s teacher or caretaker?",
    "honorific_family_friend_candidate": "Is {subject} a relative or a family friend?",
}
LOOP_WINDOW_S = 120.0  # the same words again within two minutes are one line (Whisper loops)
SPEAKER_LABEL = "the speaker"


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
    place_groups: list[dict[str, Any]] | None = None,
    geo_contexts: list[dict[str, Any]] | None = None,
    context_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Social relationship candidates, each scoped to the moments its person appears in.

    The callables come from relationships.py so both resolvers match terms and moments the same way.
    """
    child_id, family_ids = _family(kinship_candidates)
    child = next((entity for entity in person_entities if entity["id"] == child_id), None)
    others = [entity for entity in person_entities if entity["id"] not in family_ids]
    lines = _dedupe_loops(sorted(transcripts, key=lambda line: (str(line.get("source_video_id") or ""),
                                                               _number(line.get("start_s")) or 0.0)))
    for line in lines:
        line["_words"] = _words(str(line.get("text") or ""))
    event_dates = _event_dates(date_groups)
    event_regions = _event_regions(place_groups or [], geo_contexts or [])
    family_aliases = {_latin(alias) for e in person_entities if e["id"] in family_ids for alias in e.get("aliases", ())}

    unnamed: dict[str, dict[str, Any]] = {}
    said_with: dict[str, list[set[str]]] = defaultdict(list)
    spellings: dict[str, str] = {}

    def name_only(name: str, words: set[str]) -> dict[str, Any]:
        """Someone named on the soundtrack whom no person group knows yet. Spellings that differ only in a
        Russian ending (Ветрин, Ветрина) are one person, and so is the same first name in the same
        sentence with a misheard surname (Ветрина, Ветрова)."""
        spelling = _name_key(name)
        context = words - set(_WORD.findall(_latin(name)))
        key = spellings.get(spelling) or next(
            (other for other, seen in said_with.items()
             if other.split("_")[0] == spelling.split("_")[0] and any(_same_sentence(context, s) for s in seen)), spelling)
        spellings[spelling] = key
        entity = unnamed.setdefault(key, {"id": f"person_name_{key}", "label": name, "aliases": set()})
        entity["aliases"].add(name)
        said_with[key].append(context)
        return entity

    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    honorific_hits = []
    for index, line in enumerate(lines):
        text = str(line.get("text") or "")
        kinds = _social_spans(text)
        honorifics = [(m.group(1), m.group(2)) for m in HONORIFIC.finditer(text)]
        if not kinds and not honorifics:
            continue
        event = event_for_segment(events, line) or _nearest_event(events, line)
        if event is None:
            continue
        nearby = _nearby_text(lines, index, context_seconds)
        said = _with_previous(lines, index)
        tokens = _tokens(text)
        in_line = [(e, hits) for e in others if _mentions(text, e, contains_term, line["_words"])
                   if (hits := _name_hits(tokens, e))]
        specific = _most_specific(in_line)
        for kind, spans in kinds.items():
            terms = _term_positions(tokens, spans)
            # Only names said right next to the word count: in "a friend told me about him … his
            # name is Vladimir", Vladimir is not the friend.
            named = [e for e, hits in specific if _near(tokens, hits, terms)]
            source = "named_in_line"
            for name in _adjacent_names(kind, text):
                if any(_mentions(name, e, contains_term) for e in named):
                    continue
                if " " not in name and _latin(name) in family_aliases:
                    continue  # a family member's name; a surname would make it someone else
                named.append(name_only(name, line["_words"]))
                source = "named_next_to_word" if source != "named_in_line" or not named[:-1] else source
            for entity in named:
                predicate, object_id, object_label = _relation(kind, said, nearby, event, child, contains_term)
                _add(buckets, predicate, entity, object_id, object_label, line, event, kind, source)
        for title, name in honorifics:
            entity = next((e for e in others if _mentions(name, e, contains_term)), None)
            if entity is None and any(_mentions(name, e, contains_term) for e in person_entities if e["id"] in family_ids):
                continue
            honorific_hits.append((line, event, title, name, entity))

    for line, event, title, name, entity in honorific_hits:
        if entity is None:
            # Two transcripts of the same seconds can hear the name differently («дяде Олегу»,
            # «дяде Оливье»); the spelling that matches someone tapesplit knows wins.
            if any(other is not None and _same_moment(line, other_line) and other_title[:3].casefold() == title[:3].casefold()
                   for other_line, _, other_title, _, other in honorific_hits):
                continue
            entity = name_only(name, line["_words"])
        _add(buckets, "honorific_family_friend_candidate", entity, "family", "the family", line, event,
             title.casefold(), "honorific")

    family = [e for e in person_entities if e["id"] in family_ids]
    candidates = []
    for bucket in buckets.values():
        scope = _scope(bucket["entity"], events, lines, event_dates, era_contexts, family, event_for_segment,
                       contains_term, event_regions)
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


def _social_spans(text: str) -> dict[str, list[tuple[int, int]]]:
    """Each kind of relationship word in the line, with where it was said."""
    kinds = {
        "friend": [m.span() for m in FRIEND_RE.finditer(text)],
        "teacher": [m.span() for m in TEACHER_RE.finditer(text)],
        "classmate": [m.span() for pattern in CLASSMATE_PATTERNS for m in pattern.finditer(text)],
    }
    return {kind: spans for kind, spans in kinds.items() if spans}


def _adjacent_names(kind: str, text: str) -> list[str]:
    """Capitalized names said right after the word («подруга Лена») or before "our friend"."""
    if kind == "friend":
        return [m.group(1) for m in FRIEND_THEN_NAME.finditer(text)] + [m.group(1) for m in NAME_THEN_OUR_FRIEND.finditer(text)]
    if kind == "teacher":
        return [m.group(1) for m in TEACHER_THEN_NAME.finditer(text)]
    return []


def _tokens(text: str) -> list[tuple[int, int, str, int]]:
    """Each word with its span, its Latin spelling and the number of the sentence it is in."""
    tokens, sentence, last = [], 0, 0
    for match in _WORD.finditer(text):
        if SENTENCE_END.search(text, last, match.start()):
            sentence += 1
        tokens.append((match.start(), match.end(), _latin(match.group()), sentence))
        last = match.end()
    return tokens


def _word_matches(word: str, alias_word: str) -> bool:
    if word == alias_word:
        return True
    stem = alias_word[:-1] if alias_word[-1] in "aeiouy" else alias_word
    return len(stem) >= 4 and word.startswith(stem) and len(word) - len(stem) <= 3


def _name_hits(tokens: list[tuple[int, int, str, int]], entity: dict[str, Any]) -> list[tuple[int, int]]:
    """Where the line names this person, as (first word, last word)."""
    hits = []
    for alias in entity.get("aliases", ()):
        parts = _WORD.findall(_latin(alias))
        if not parts or (len(parts) == 1 and len(parts[0]) < 3):
            continue
        for i in range(len(tokens) - len(parts) + 1):
            if all(_word_matches(tokens[i + k][2], part) for k, part in enumerate(parts)):
                hits.append((i, i + len(parts) - 1))
    return hits


def _most_specific(named: list[tuple[dict[str, Any], list[tuple[int, int]]]]) -> list[tuple[dict[str, Any], list[tuple[int, int]]]]:
    """Drop a person named only inside a longer name for someone else ("Ольга" in "Ольга Николаевна")."""
    def inside(span: tuple[int, int], other: dict[str, Any]) -> bool:
        return any(a <= span[0] and span[1] <= b and b - a > span[1] - span[0]
                   for e, hits in named if e is not other for a, b in hits)
    return [(e, hits) for e, hits in named if not all(inside(span, e) for span in hits)]


def _term_positions(tokens: list[tuple[int, int, str, int]], spans: list[tuple[int, int]]) -> list[int]:
    return [i for i, (a, b, _, _) in enumerate(tokens) if any(a < end and start < b for start, end in spans)]


def _near(tokens: list[tuple[int, int, str, int]], hits: list[tuple[int, int]], terms: list[int]) -> bool:
    return any(tokens[term][3] == tokens[first][3] and min(abs(term - first), abs(term - last)) <= NEAR_WORDS
               for first, last in hits for term in terms)


def _same_moment(a: dict[str, Any], b: dict[str, Any]) -> bool:
    start_a, start_b = _number(a.get("start_s")), _number(b.get("start_s"))
    return (a.get("source_video_id") == b.get("source_video_id") and start_a is not None and start_b is not None
            and abs(start_a - start_b) <= SAME_MOMENT_S)


def _latin(text: str) -> str:
    return _transliterate_cyrillic(str(text).casefold())


def _words(text: str) -> set[str]:
    """Each word in Latin letters, so «Гриша» meets an alias spelled Grisha."""
    return {_latin(w) for w in _WORD.findall(text)}


def _mentions(text: str, entity: dict[str, Any], contains_term: Any, words: set[str] | None = None) -> bool:
    """Whole-word and case-insensitive, across Cyrillic and Latin, allowing a Russian case ending."""
    words = _words(text) if words is None else words
    for alias in entity.get("aliases", ()):
        alias = _latin(alias)
        if len(alias) < 3:
            continue
        if " " in alias or "-" in alias:
            if contains_term(_latin(text), alias):
                return True
            continue
        if alias in words:
            return True
        stem = alias[:-1] if alias[-1] in "aeiouy" else alias
        if len(stem) >= 4 and any(word.startswith(stem) and len(word) - len(stem) <= 3 for word in words):
            return True
    return False


def _name_key(name: str) -> str:
    """A name without its Russian case or gender ending: Ветрин and Ветрина give the same key."""
    return "_".join(word[:-1] if len(word) > 3 and word[-1] in "aeiouy" else word for word in _WORD.findall(_latin(name)))


NEAREST_EVENT_MAX_S = 900.0  # the video model's moment edges drift several minutes from the speech


def _nearest_event(events: list[dict[str, Any]], line: dict[str, Any]) -> dict[str, Any] | None:
    """The closest moment on the same tape, for a line said between moments."""
    video = line.get("source_video_id")
    start = _number(line.get("start_s"))
    if not video or start is None:
        return None
    best, best_gap = None, NEAREST_EVENT_MAX_S
    for event in events:
        metadata = event.get("metadata") or {}
        for item in metadata.get("source_ranges") or []:
            if item.get("source_video_id") != video:
                continue
            a, b = _number(item.get("start_s")), _number(item.get("end_s"))
            if a is None or b is None:
                continue
            gap = 0.0 if a <= start <= b else min(abs(start - a), abs(start - b))
            if gap <= best_gap:
                best, best_gap = event, gap
    return best


def _with_previous(lines: list[dict[str, Any]], index: int) -> str:
    """The line, plus the one before it when it carries on that sentence («…моя учительница физики,»
    then «а также директриса Лариса.»): transcripts cut sentences into pieces."""
    line = lines[index]
    text = str(line.get("text") or "").strip()
    start = _number(line.get("start_s"))
    if not text[:1].islower() or start is None:
        return text
    for previous in reversed(lines[max(0, index - 6):index]):
        if (previous.get("source_video_id"), previous.get("observation_source")) != (
                line.get("source_video_id"), line.get("observation_source")):
            continue
        end = _number(previous.get("end_s"))
        if end is not None and -0.5 <= start - end <= 3.0:
            return str(previous.get("text") or "").strip() + " " + text
        break
    return text


def _same_sentence(a: set[str], b: set[str]) -> bool:
    """Nearly the same words around a name: the same sentence, heard twice."""
    return min(len(a), len(b)) >= 4 and len(a & b) / min(len(a), len(b)) >= 0.75


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
            return "teacher_or_caretaker_candidate", "speaker", SPEAKER_LABEL
        return "teacher_or_caretaker_candidate", child_id, child_label
    if kind == "classmate":
        return "friend_or_classmate_candidate", child_id, child_label
    if mine and not ours:
        return "friend_or_classmate_candidate", "speaker", SPEAKER_LABEL
    if ours:
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


def _event_regions(place_groups: list[dict[str, Any]], geo_contexts: list[dict[str, Any]]) -> dict[str, set[tuple[str, str]]]:
    """For each moment, the (country, state or country name) of the places tapesplit found in it."""
    region_by_place: dict[str, tuple[str, str]] = {}
    for geo in geo_contexts:
        region = geo.get("region") if isinstance(geo.get("region"), dict) else {}
        country = str(region.get("country") or "")
        if not country:
            continue
        if country == "us":
            name = str(region.get("state") or "").title()
        else:
            name = str(geo.get("region_label") or "").split(",")[-1].strip()
        if name:
            region_by_place[str(geo.get("place_group_id") or "")] = (country, name)
    regions: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for place in place_groups:
        region = region_by_place.get(str(place.get("id") or ""))
        if region:
            for event_id in place.get("canonical_event_ids") or []:
                regions[str(event_id)].add(region)
    return regions


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
           event_for_segment: Any, contains_term: Any,
           event_regions: dict[str, set[tuple[str, str]]] | None = None) -> dict[str, Any]:
    """Every moment this person turns up in: named by the video model, or named on the soundtrack."""
    moments: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_has(event, entity):
            moments[str(event.get("id"))] = event
    for line in lines:
        if _mentions(str(line.get("text") or ""), entity, contains_term, line.get("_words")):
            event = event_for_segment(events, line) or _nearest_event(events, line)
            if event is not None:
                moments[str(event.get("id"))] = event

    dates, contexts, tapes, with_family = [], Counter(), set(), Counter()
    for event_id, event in moments.items():
        metadata = event.get("metadata") or {}
        date = event_dates.get(event_id)
        year = int(date[:4]) if date and date[:4].isdigit() else None
        if date:
            dates.append(date)
        era = _era_for_year(year, era_contexts)
        home = era.split(",")[-1].strip() if era else ""
        away = sorted(name for _, name in (event_regions or {}).get(event_id, set()) if home and name != home)
        if away:
            contexts[f"a trip: {away[0]}"] += 1
        elif str(metadata.get("event_type") or "") == "travel":
            contexts["a trip"] += 1
        else:
            contexts[era or "at home, year unknown"] += 1
        for video_id in metadata.get("source_video_ids") or []:
            tapes.add(str(video_id))
        for member in family:
            if _event_has(event, member):
                with_family[member["label"]] += 1
    dates.sort()
    only_trips = bool(moments) and all(label.startswith("a trip") for label in contexts)
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

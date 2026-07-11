"""Grounded journal posts: machine-drafted narrative with enforced citations.

A journal post is the archive telling one day's story — warm, specific,
first-person-family — while remaining a grounding layer: every factual
sentence cites the event/segment ids it derives from, every pull-quote is a
verbatim transcript span, and every entity mention resolves to a real
person/place id. Blocks that cannot prove their grounding are rejected at
generation time (one regeneration round with the violations as feedback,
then dropped) — the same philosophy as the clip-verification loop, applied
before publication instead of after.

Structural conventions (kicker/title/dek/hero front-matter, typed block
stream, provenance derived from block citations rather than hand-written,
machine-drafted disclosure) follow Phillip's sphre briefs; the voice and
visual identity are journal-native (see docs/JOURNAL.md).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from tapesplit.claim_store import DualWriter
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.story import _dedupe_albums, _event_source_ranges
from tapesplit.visibility import build_visibility_filter

JOURNAL_POSTS_FILENAME = "journal_posts.jsonl"
DEFAULT_JOURNAL_DEPLOYMENT = "gpt-5.6-terra"

BLOCK_TYPES = {"heading", "paragraph", "pullquote", "clip"}
ENTITY_KINDS = {"person", "place", "event"}

# Albums bigger than this read as trips, not days; posts stay day-sized.
MAX_PACKET_EVENTS = 24
MAX_PACKET_QUOTES = 12
MIN_QUOTE_WORDS = 3
MAX_QUOTE_CHARS = 160
REGENERATION_ROUNDS = 1

# --- Memory salience ------------------------------------------------------
# A quote earns its place by carrying a memory, not by parsing cleanly.
# Layer 1 scores lexical signals of memorable moments (bilingual — the
# archive narrates in Russian and English); layer 2 scores semantic
# similarity to memory archetypes; layer 3 (terra) picks the finalists and
# says why. Weights were tuned by reading the generated posts against the
# bar "would this give Phillip a memory".
SALIENCE_FIRST = 3.0
SALIENCE_MILESTONE = 2.0
SALIENCE_WONDER = 1.5
SALIENCE_CHILD_VOICE = 1.5
SALIENCE_NAMED = 1.0
SALIENCE_DIRECT_ADDRESS = 1.0
SALIENCE_EMOTIVE = 1.0
SALIENCE_PLAY = 1.0
SALIENCE_EXCLAIM = 0.75
SALIENCE_PROCEDURAL = -2.0
SALIENCE_CAMERA_TALK = -1.5
SALIENCE_FILLER = -2.0
SEMANTIC_SALIENCE_SCALE = 4.0  # (cosine - floor) * scale, capped at 2.0
SEMANTIC_SALIENCE_FLOOR = 0.25
HALLUCINATION_REPEAT_LIMIT = 3  # same normalized text N+ times in one tape = whisper loop

MEMORY_ARCHETYPES = [
    "a child seeing something wonderful for the first time",
    "ребёнок впервые видит что-то удивительное",
    "making a birthday wish before blowing out the candles",
    "поздравление с днём рождения, задувание свечей",
    "a parent saying something tender to their child",
    "мама или папа говорит ребёнку что-то нежное",
    "a joke or a silly moment that made the whole family laugh",
    "шутка или глупость, над которой смеялась вся семья",
    "a child proudly showing what they made or learned",
    "ребёнок с гордостью показывает, что он сделал или чему научился",
    "losing a tooth, learning to ride, the first day of school",
    "выпал зуб, первый день в школе, научился кататься",
    "singing a song together",
    "поём песню все вместе",
    "saying goodbye or greeting someone dearly missed",
    "прощание или встреча с тем, по кому скучали",
]

_FIRST_RE = None  # compiled lazily in _salience_patterns()

Generator = Callable[[dict[str, Any], list[str] | None], dict[str, Any]]
Ranker = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, str]]


def generate_journal_posts(
    project_dir: Path,
    *,
    limit: int = 8,
    album_ids: list[str] | None = None,
    deployment: str = DEFAULT_JOURNAL_DEPLOYMENT,
    generator: Generator | None = None,
    ranker: Ranker | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    packets = build_grounding_packets(project, limit=limit, album_ids=album_ids)
    if dry_run:
        return {
            "project": str(project),
            "dry_run": True,
            "packets": [
                {
                    "album_id": packet["album_id"],
                    "title_hint": packet["title_hint"],
                    "events": len(packet["events"]),
                    "quotes": len(packet["quotes"]),
                    "people": len(packet["people"]),
                    "places": len(packet["places"]),
                }
                for packet in packets
            ],
        }

    generate = generator or _terra_generator(project, deployment)
    # Live runs get the terra editorial pass; injected generators (tests)
    # only rank when a ranker is injected alongside.
    rank = ranker if ranker is not None else (_terra_ranker(project, deployment) if generator is None else None)
    existing_rows = read_jsonl(project / JOURNAL_POSTS_FILENAME)
    if force:
        wanted = {packet["album_id"] for packet in packets}
        kept = [row for row in existing_rows if row.get("album_id") not in wanted]
        path = project / JOURNAL_POSTS_FILENAME
        if path.exists():
            path.unlink()
        for row in kept:
            append_jsonl(path, row)
        existing_rows = kept
    existing = {row.get("album_id") for row in existing_rows}
    posts: list[dict[str, Any]] = []
    rejected_total = 0
    block_total = 0
    skipped: list[dict[str, str]] = []
    for packet in packets:
        if packet["album_id"] in existing:
            skipped.append({"album_id": packet["album_id"], "reason": "post already exists"})
            continue
        if rank is not None:
            _apply_quote_ranking(packet, rank)
        try:
            post, rejected = _generate_one(packet, generate)
        except Exception as error:  # generation is best-effort per album
            skipped.append({"album_id": packet["album_id"], "reason": str(error)[:200]})
            continue
        rejected_total += len(rejected)
        block_total += len(post["blocks"]) + len(rejected)
        posts.append(post)

    if posts:
        for post in posts:
            append_jsonl(project / JOURNAL_POSTS_FILENAME, post)
        writer = DualWriter.open(project, artifact=JOURNAL_POSTS_FILENAME, producer=f"journal/{deployment}")
        for post in posts:
            span = post.get("source_span") or {}
            writer.write_row(
                post,
                kind="event",
                media_id=span.get("source_video_id"),
                start_s=span.get("start_s"),
                end_s=span.get("end_s"),
                confidence=post.get("confidence"),
                assertion={
                    "journal_post": post["id"],
                    "title": post["title"],
                    "citations": sorted(_post_citations(post)),
                },
            )
        writer.close()
        _persist_salience(project, packets, generated={post["album_id"] for post in posts})

    return {
        "project": str(project),
        "posts_written": len(posts),
        "post_ids": [post["id"] for post in posts],
        "blocks": block_total,
        "rejected_blocks": rejected_total,
        "rejected_block_rate": round(rejected_total / block_total, 3) if block_total else 0.0,
        "skipped": skipped,
        "output": str(project / JOURNAL_POSTS_FILENAME),
    }


def list_journal_posts(project_dir: Path) -> list[dict[str, Any]]:
    project = project_dir.expanduser().resolve()
    return [
        {
            "id": row.get("id"),
            "album_id": row.get("album_id"),
            "kicker": row.get("kicker"),
            "title": row.get("title"),
            "date_label": row.get("date_label"),
            "blocks": len(row.get("blocks") or []),
            "read_minutes": row.get("read_minutes"),
        }
        for row in read_jsonl(project / JOURNAL_POSTS_FILENAME)
    ]


# ---------------------------------------------------------------------------
# Grounding packets


def build_grounding_packets(
    project: Path,
    *,
    limit: int = 8,
    album_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    evidence_by_id = {
        row.get("id"): row
        for row in read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
        if row.get("id")
    }
    visibility = build_visibility_filter(project)
    events_by_id = {
        str(row.get("id") or ""): row
        for row in read_jsonl(project / "canonical_events.jsonl")
        if row.get("id") and visibility.visible_row(row, evidence_by_id=evidence_by_id)
    }
    reconciliations = {
        str(row.get("canonical_event_id") or ""): row
        for row in read_jsonl(project / "event_reconciliations.jsonl")
        if row.get("canonical_event_id")
    }
    segments_by_video: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(project / "speaker_segments.jsonl"):
        text = str((row.get("metadata") or {}).get("transcript_text") or "").strip()
        if not text:
            continue
        segments_by_video.setdefault(str(row.get("source_video_id") or ""), []).append(row)
    people_groups = [row for row in read_jsonl(project / "people_groups.jsonl") if row.get("id")]
    place_roles_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(project / "event_place_roles.jsonl"):
        if str(row.get("review_status") or "") == "rejected" or not row.get("label"):
            continue
        place_roles_by_event.setdefault(str(row.get("canonical_event_id") or ""), []).append(row)

    albums = [
        row
        for row in read_jsonl(project / "albums.jsonl")
        if row.get("date_label")
        and row.get("canonical_event_ids")
        and str(row.get("export_status") or "") != "excluded"
        and visibility.visible_row(row, evidence_by_id=evidence_by_id)
    ]
    albums = _dedupe_albums(albums)
    if album_ids:
        wanted = {str(album_id) for album_id in album_ids}
        albums = [album for album in albums if str(album.get("id")) in wanted]
    repeat_counts = _segment_repeat_counts(segments_by_video)
    child_speakers = _child_speakers(project, people_groups)
    archetype_scorer = _archetype_scorer_for(project)
    packets = []
    for album in albums:
        packet = _packet_for_album(
            album,
            events_by_id=events_by_id,
            reconciliations=reconciliations,
            segments_by_video=segments_by_video,
            people_groups=people_groups,
            place_roles_by_event=place_roles_by_event,
            repeat_counts=repeat_counts,
            child_speakers=child_speakers,
            archetype_scorer=archetype_scorer,
        )
        if packet is not None:
            packets.append(packet)
    # Richest first: memorable quotes are what make a post worth reading.
    packets.sort(
        key=lambda p: (sum(q.get("salience", 0.0) for q in p["quotes"][:4]), len(p["events"])),
        reverse=True,
    )
    return packets[: max(0, int(limit))] if not album_ids else packets


def _packet_for_album(
    album: dict[str, Any],
    *,
    events_by_id: dict[str, dict[str, Any]],
    reconciliations: dict[str, dict[str, Any]],
    segments_by_video: dict[str, list[dict[str, Any]]],
    people_groups: list[dict[str, Any]],
    place_roles_by_event: dict[str, list[dict[str, Any]]],
    repeat_counts: dict[str, dict[str, int]] | None = None,
    child_speakers: set[str] | None = None,
    archetype_scorer: Callable[[list[str]], list[float]] | None = None,
) -> dict[str, Any] | None:
    event_ids = [str(event_id) for event_id in album.get("canonical_event_ids") or [] if event_id]
    events = [events_by_id[event_id] for event_id in event_ids if event_id in events_by_id]
    if len(events) < 2 or len(events) > MAX_PACKET_EVENTS:
        return None

    event_entries = []
    ranges_by_video: dict[str, list[tuple[float, float]]] = {}
    for event in events:
        event_id = str(event.get("id"))
        reconciliation = reconciliations.get(event_id) or {}
        source_ranges = _event_source_ranges(event)
        for row in source_ranges:
            start = _number(row.get("start_s"))
            end = _number(row.get("end_s"))
            if start is None or end is None:
                continue
            ranges_by_video.setdefault(row["source_video_id"], []).append((start, end))
        event_entries.append(
            {
                "id": event_id,
                "title": str(reconciliation.get("reconciled_title") or event.get("title") or "Untitled"),
                "summary": str(reconciliation.get("reconciled_summary") or event.get("summary") or "")[:400],
                "source_ranges": source_ranges,
                "confidence": event.get("confidence"),
            }
        )

    quotes = _select_quotes(
        ranges_by_video,
        segments_by_video,
        repeat_counts=repeat_counts,
        child_speakers=child_speakers,
        archetype_scorer=archetype_scorer,
    )
    people = _people_for_events(set(str(e["id"]) for e in event_entries), people_groups)
    places = _places_for_events(event_entries, place_roles_by_event)
    source_span = _album_source_span(ranges_by_video)

    return {
        "album_id": str(album.get("id")),
        "album_type": album.get("album_type"),
        "title_hint": str(album.get("title") or ""),
        "date_label": str(album.get("date_label") or ""),
        "date_confidence": album.get("confidence"),
        "events": event_entries,
        "quotes": quotes,
        "people": people,
        "places": places,
        "cover_event_id": str(album.get("cover_event_id") or "") or (event_entries[0]["id"] if event_entries else None),
        "source_span": source_span,
    }


def _select_quotes(
    ranges_by_video: dict[str, list[tuple[float, float]]],
    segments_by_video: dict[str, list[dict[str, Any]]],
    *,
    repeat_counts: dict[str, dict[str, int]] | None = None,
    child_speakers: set[str] | None = None,
    archetype_scorer: Callable[[list[str]], list[float]] | None = None,
) -> list[dict[str, Any]]:
    repeat_counts = repeat_counts or {}
    child_speakers = child_speakers or set()
    candidates = []
    for video_id, ranges in ranges_by_video.items():
        video_repeats = repeat_counts.get(video_id) or {}
        for segment in segments_by_video.get(video_id, []):
            start = _number(segment.get("start_s"))
            end = _number(segment.get("end_s"))
            if start is None or end is None:
                continue
            if not any(start < r_end and end > r_start for r_start, r_end in ranges):
                continue
            text = str((segment.get("metadata") or {}).get("transcript_text") or "").strip()
            words = len(text.split())
            if words < MIN_QUOTE_WORDS or len(text) > MAX_QUOTE_CHARS:
                continue
            # Whisper hallucination loops repeat one line dozens of times;
            # a "quote" that occurs 3+ times in a tape is machinery, not memory.
            if video_repeats.get(_normalized_text_key(text), 0) >= HALLUCINATION_REPEAT_LIMIT:
                continue
            speaker = str(segment.get("speaker_label") or "")
            named = bool(speaker) and not speaker.startswith("AZ_SPEAKER")
            score, reasons = _salience_heuristic(
                text, named=named, child=named and speaker.casefold() in child_speakers
            )
            candidates.append(
                {
                    "segment_id": str(segment.get("id")),
                    "speaker": speaker if named else "",
                    "text": text,
                    "source_video_id": video_id,
                    "start_s": start,
                    "end_s": end,
                    "salience": score,
                    "salience_reasons": reasons,
                }
            )
    if candidates and archetype_scorer is not None:
        try:
            semantic = archetype_scorer([row["text"] for row in candidates])
        except Exception:
            semantic = [0.0] * len(candidates)  # embeddings are an upgrade, never a dependency
        for row, boost in zip(candidates, semantic, strict=True):
            if boost > 0:
                row["salience"] = round(row["salience"] + boost, 3)
                row["salience_reasons"] = row["salience_reasons"] + ["memory-archetype"]
    candidates.sort(key=lambda row: (row["salience"], row["start_s"] or 0.0), reverse=True)
    return candidates[:MAX_PACKET_QUOTES]


def _salience_patterns() -> dict[str, Any]:
    global _FIRST_RE
    if _FIRST_RE is None:
        import re

        def rx(pattern: str) -> Any:
            return re.compile(pattern, re.IGNORECASE)

        _FIRST_RE = {
            "first": rx(r"перв\w* раз|впервые|first time|for the first time"),
            "milestone": rx(
                r"день рождени|с днём|с днем|исполнилось|задува|свеч|birthday|"
                r"зуб\b|зубик|tooth|школ\w|first day|новый год|рождеств|christmas|ёлк|елк"
            ),
            "wonder": rx(r"ух ты|ура|вот это|смотри|смотрите|гляди|погляди|look|wow|whoa|üра"),
            "address": rx(r"\bмам\w?\b|\bпап\w?\b|\bбабушк|\bдедушк|mama|papa|mommy|daddy|grandma|grandpa"),
            "emotive": rx(
                r"любл|люби|красив|страшн|смешн|весел|счастлив|боюсь|обожа|нрав|скуча|"
                r"love|beautiful|funny|scared|happy|miss you|proud"
            ),
            "play": rx(r"пою|поём|поем|споём|споем|песн|танцу|прыга|каталис|катаемся|sing|song|dance|jump"),
            "procedural": rx(
                r"передай|возьми|положи|поставь|садись|сядь|иди сюда|подожди|подвин|"
                r"не трогай|быстрее|pass the|sit down|come here|hold on|hurry"
            ),
            "camera": rx(r"камер|снима|плёнк|пленк|кассет|батарейк|record|camera|filming|tape\b"),
        }
    return _FIRST_RE


def _salience_heuristic(text: str, *, named: bool, child: bool) -> tuple[float, list[str]]:
    patterns = _salience_patterns()
    score = 0.0
    reasons: list[str] = []

    def hit(key: str, points: float) -> None:
        nonlocal score
        if patterns[key].search(text):
            score += points
            reasons.append(key)

    hit("first", SALIENCE_FIRST)
    hit("milestone", SALIENCE_MILESTONE)
    hit("wonder", SALIENCE_WONDER)
    hit("address", SALIENCE_DIRECT_ADDRESS)
    hit("emotive", SALIENCE_EMOTIVE)
    hit("play", SALIENCE_PLAY)
    hit("procedural", SALIENCE_PROCEDURAL)
    hit("camera", SALIENCE_CAMERA_TALK)
    words = text.split()
    if named:
        score += SALIENCE_NAMED
        reasons.append("named-speaker")
    if child:
        score += SALIENCE_CHILD_VOICE
        reasons.append("child-voice")
    if text.rstrip().endswith(("!", "?!")) and len(words) <= 10:
        score += SALIENCE_EXCLAIM
        reasons.append("exclaim")
    if len(set(word.casefold() for word in words)) <= 2 or sum(ch.isdigit() for ch in text) > len(text) / 4:
        score += SALIENCE_FILLER
        reasons.append("filler")
    return round(score, 3), reasons


def _normalized_text_key(text: str) -> str:
    import re

    return re.sub(r"[\W\d_]+", " ", text.casefold()).strip()


def _segment_repeat_counts(segments_by_video: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for video_id, segments in segments_by_video.items():
        per_video: dict[str, int] = {}
        for segment in segments:
            text = str((segment.get("metadata") or {}).get("transcript_text") or "").strip()
            if not text:
                continue
            key = _normalized_text_key(text)
            if key:
                per_video[key] = per_video.get(key, 0) + 1
        counts[video_id] = per_video
    return counts


def _child_speakers(project: Path, people_groups: list[dict[str, Any]]) -> set[str]:
    """Speaker labels belonging to people whose CONFIRMED age model says child.

    Candidate-basis age models are polluted by event co-occurrence (a birthday
    averages the guests), so only confirmed attributions vote — the set grows
    as review decisions land, and an empty set just means no child boost yet.
    """
    aliases_by_group: dict[str, list[str]] = {
        str(group.get("id")): [str(a) for a in group.get("aliases") or []] + [str(group.get("label") or "")]
        for group in people_groups
    }
    labels: set[str] = set()
    for row in read_jsonl(project / "person_age_models.jsonl"):
        if row.get("adult") is not False or str(row.get("attribution_basis") or "") != "confirmed":
            continue
        for alias in aliases_by_group.get(str(row.get("person_group_id") or ""), []):
            for part in alias.split("/"):
                if part.strip():
                    labels.add(part.strip().casefold())
    return labels


def _archetype_scorer_for(project: Path) -> Callable[[list[str]], list[float]] | None:
    """Memory-archetype similarity via the semantic-search embedding stack.

    Reuses the project's content-hash embedding cache so repeat scoring is
    free; returns None when sentence-transformers is unavailable. Gated on
    the cache db existing (i.e. semantic search has run on this project) so
    small projects and unit tests never pay a model load.
    """
    try:
        from tapesplit.semantic_search import SEMANTIC_CACHE_DB_NAME as _cache_name

        if not (project / _cache_name).exists():
            return None
    except Exception:
        return None
    try:
        from tapesplit.search import DEFAULT_EMBEDDING_MODEL
        from tapesplit.semantic_search import (
            SEMANTIC_CACHE_DB_NAME,
            _cached_vectors,
            _content_hash,
            _ensure_cache_schema,
            _sentence_transformer_encoder,
        )

        encode = _sentence_transformer_encoder(DEFAULT_EMBEDDING_MODEL)
    except Exception:
        return None
    import json as json_module
    import sqlite3

    archetype_vectors = encode(MEMORY_ARCHETYPES)

    def score(texts: list[str]) -> list[float]:
        cache = sqlite3.connect(project / SEMANTIC_CACHE_DB_NAME)
        try:
            _ensure_cache_schema(cache)
            hashes = [_content_hash(text) for text in texts]
            found = _cached_vectors(cache, hashes, DEFAULT_EMBEDDING_MODEL)
            missing = [(digest, text) for digest, text in zip(hashes, texts, strict=True) if digest not in found]
            if missing:
                fresh = encode([text for _, text in missing])
                for (digest, _), vector in zip(missing, fresh, strict=True):
                    found[digest] = vector
                    cache.execute(
                        "INSERT OR REPLACE INTO text_embeddings (hash, model, dim, vector_json) VALUES (?, ?, ?, ?)",
                        (digest, DEFAULT_EMBEDDING_MODEL, len(vector), json_module.dumps(vector)),
                    )
                cache.commit()
        finally:
            cache.close()
        scores = []
        for digest in hashes:
            vector = found.get(digest)
            if not vector:
                scores.append(0.0)
                continue
            best = max(
                sum(a * b for a, b in zip(vector, archetype, strict=True))
                for archetype in archetype_vectors
            )
            scores.append(round(min(2.0, max(0.0, (best - SEMANTIC_SALIENCE_FLOOR) * SEMANTIC_SALIENCE_SCALE)), 3))
        return scores

    return score


def _people_for_events(event_ids: set[str], people_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    people = []
    for group in people_groups:
        overlap = event_ids & {str(event_id) for event_id in group.get("canonical_event_ids") or []}
        if not overlap:
            continue
        aliases = [str(alias) for alias in group.get("aliases") or [] if alias]
        people.append(
            {
                "id": str(group.get("id")),
                "label": _display_name(str(group.get("label") or ""), aliases),
                "aliases": aliases[:8],
                "events_here": len(overlap),
            }
        )
    people.sort(key=lambda row: row["events_here"], reverse=True)
    return people[:10]


def _places_for_events(
    event_entries: list[dict[str, Any]],
    place_roles_by_event: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    by_label: dict[str, dict[str, Any]] = {}
    for event in event_entries:
        for role in place_roles_by_event.get(event["id"], []):
            label = str(role.get("label") or "").strip()
            key = label.casefold()
            if not key:
                continue
            entry = by_label.setdefault(
                key, {"id": str(role.get("id")), "label": label, "event_ids": []}
            )
            entry["event_ids"].append(event["id"])
    places = sorted(by_label.values(), key=lambda row: len(row["event_ids"]), reverse=True)
    return places[:6]


def _album_source_span(ranges_by_video: dict[str, list[tuple[float, float]]]) -> dict[str, Any] | None:
    if len(ranges_by_video) != 1:
        return None
    video_id, ranges = next(iter(ranges_by_video.items()))
    if not ranges:
        return None
    return {
        "source_video_id": video_id,
        "start_s": min(start for start, _ in ranges),
        "end_s": max(end for _, end in ranges),
    }


def _display_name(label: str, aliases: list[str]) -> str:
    candidates = [part.strip() for part in label.split("/")] + aliases
    latin = [c for c in candidates if c and all(ord(ch) < 0x400 for ch in c)]
    pool = latin or [c for c in candidates if c]
    return min(pool, key=len) if pool else label


# ---------------------------------------------------------------------------
# Terra quote ranking: a cheap editorial pass that picks the finalists and
# says WHY each one matters — the why becomes the quote's micro-context.

RANKER_MAX_CANDIDATES = 15
RANKER_WHY_MAX_CHARS = 90

RANKER_PROMPT = """You are choosing pull-quotes for a family journal entry built from home-video
transcripts. Pick the 2-4 quotes most likely to hand a family member a MEMORY: firsts, milestones,
a child's wonder, tenderness, a laugh — a concrete moment, never logistics or narration about
filming. For each pick, say in one short clause what is happening when it is said (the reader sees
this as context). Respond as JSON only:
{"picks": [{"segment_id": str, "why": str (<= 12 words, concrete, present tense)}]}"""


def _apply_quote_ranking(packet: dict[str, Any], ranker: Ranker) -> None:
    candidates = packet["quotes"][:RANKER_MAX_CANDIDATES]
    if not candidates:
        return
    try:
        picks = ranker(packet, candidates)
    except Exception:
        return  # salience order stands; ranking is an upgrade, not a dependency
    known = {quote["segment_id"] for quote in candidates}
    picks = {
        segment_id: str(why)[:RANKER_WHY_MAX_CHARS].strip()
        for segment_id, why in picks.items()
        if segment_id in known and str(why).strip()
    }
    if not picks:
        return
    for quote in packet["quotes"]:
        if quote["segment_id"] in picks:
            quote["why"] = picks[quote["segment_id"]]
    packet["quotes"].sort(key=lambda quote: (quote["segment_id"] not in picks, -quote.get("salience", 0.0)))


def _terra_ranker(project: Path, deployment: str) -> Ranker:
    from tapesplit.azure_openai_adapter import reasoning_chat_completion

    def rank(packet: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, str]:
        album_line = f"{packet.get('title_hint') or 'A day'} — {packet.get('date_label') or 'undated'}; events: " + "; ".join(
            event["title"] for event in packet["events"][:8]
        )
        payload = [
            {"segment_id": quote["segment_id"], "speaker": quote.get("speaker") or "?", "text": quote["text"]}
            for quote in candidates
        ]
        result = reasoning_chat_completion(
            deployment=deployment,
            messages=[
                {"role": "system", "content": RANKER_PROMPT},
                {"role": "user", "content": album_line + "\n" + json.dumps(payload, ensure_ascii=False)},
            ],
            max_completion_tokens=600,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            project_dir=project,
            operation="journal_rank_quotes",
        )
        parsed = json.loads(result["choices"][0]["message"]["content"])
        return {
            str(pick.get("segment_id") or ""): str(pick.get("why") or "")
            for pick in parsed.get("picks") or []
            if isinstance(pick, dict)
        }

    return rank


# ---------------------------------------------------------------------------
# Generation + the grounding contract


def _generate_one(packet: dict[str, Any], generate: Generator) -> tuple[dict[str, Any], list[dict[str, str]]]:
    feedback: list[str] | None = None
    post_raw: dict[str, Any] = {}
    clean_blocks: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for _ in range(1 + REGENERATION_ROUNDS):
        post_raw = generate(packet, feedback)
        clean_blocks, rejected = _validate_blocks(post_raw.get("blocks") or [], packet)
        if not rejected:
            break
        feedback = [f"block {index}: {reason}" for index, reason in enumerate(r["reason"] for r in rejected)]
    if not clean_blocks:
        raise ValueError("no blocks survived the grounding contract")

    title = str(post_raw.get("title") or packet["title_hint"] or "Untitled day").strip()
    post = {
        "id": f"journal_post_{packet['album_id']}",
        "album_id": packet["album_id"],
        "kicker": str(post_raw.get("kicker") or _default_kicker(packet)).strip()[:40],
        "title": title[:90],
        "dek": str(post_raw.get("dek") or "").strip()[:200],
        "date_label": packet["date_label"],
        "hero_event_id": _valid_event_id(post_raw.get("hero_event_id"), packet) or packet.get("cover_event_id"),
        "blocks": clean_blocks,
        "citations": sorted(_blocks_citations(clean_blocks)),
        "generated": True,
        "generator": str(post_raw.get("_generator") or ""),
        "confidence": packet.get("date_confidence"),
        "source_span": packet.get("source_span"),
        "read_minutes": _read_minutes(clean_blocks),
        "rejected_blocks": len(rejected),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    return post, rejected


def _validate_blocks(
    blocks: list[Any], packet: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    event_ids = {event["id"] for event in packet["events"]}
    quote_by_id = {quote["segment_id"]: quote for quote in packet["quotes"]}
    entity_ids = (
        {person["id"] for person in packet["people"]}
        | {place["id"] for place in packet["places"]}
        | event_ids
    )
    citable = event_ids | set(quote_by_id)
    clean: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []

    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in BLOCK_TYPES:
            rejected.append({"reason": f"unknown block shape/type: {str(block)[:60]}"})
            continue
        kind = block["type"]
        text = str(block.get("text") or "").strip()
        citations = [str(c) for c in block.get("citations") or [] if c]
        reason = None

        if kind == "heading":
            if not text:
                reason = "empty heading"
        elif kind == "paragraph":
            if not text:
                reason = "empty paragraph"
            elif not citations:
                reason = "paragraph asserts facts with no citations"
            elif not set(citations) <= citable:
                reason = f"unknown citation ids: {sorted(set(citations) - citable)[:3]}"
        elif kind == "pullquote":
            segment = quote_by_id.get(str(block.get("segment_id") or ""))
            if segment is None:
                reason = "pullquote must cite a provided quote segment_id"
            elif _normalize_ws(text) not in _normalize_ws(segment["text"]) and _normalize_ws(
                segment["text"]
            ) not in _normalize_ws(text):
                reason = "pullquote text is not verbatim from the cited segment"
            else:
                block = {
                    **block,
                    "speaker": segment["speaker"],
                    "source_video_id": segment["source_video_id"],
                    "start_s": segment["start_s"],
                    "end_s": segment["end_s"],
                    "citations": [segment["segment_id"]],
                    "context": str(segment.get("why") or ""),
                    "salience": segment.get("salience"),
                }
        elif kind == "clip":
            event_id = _valid_event_id(block.get("event_id"), packet)
            if event_id is None:
                reason = "clip must reference a packet event id"
            else:
                block = {**block, "event_id": event_id, "citations": [event_id]}

        if reason is None:
            entities, entity_reason = _validate_entities(block.get("entities") or [], text, entity_ids)
            if entity_reason:
                reason = entity_reason
            else:
                block = {**block, "entities": entities}

        if reason:
            rejected.append({"reason": reason})
        else:
            clean.append(block)
    return clean, rejected


def _validate_entities(
    entities: list[Any], text: str, entity_ids: set[str]
) -> tuple[list[dict[str, str]], str | None]:
    clean = []
    for entity in entities:
        if not isinstance(entity, dict):
            return [], "entity is not an object"
        span_text = str(entity.get("span_text") or "")
        kind = str(entity.get("kind") or "")
        entity_id = str(entity.get("id") or "")
        if kind not in ENTITY_KINDS:
            return [], f"unknown entity kind {kind!r}"
        if entity_id not in entity_ids:
            return [], f"entity id {entity_id!r} not in packet roster"
        if span_text and span_text not in text:
            return [], f"entity span {span_text!r} not present in block text"
        clean.append({"span_text": span_text, "kind": kind, "id": entity_id})
    return clean, None


JOURNAL_SALIENCE_FILENAME = "journal_salience.jsonl"


def _persist_salience(project: Path, packets: list[dict[str, Any]], *, generated: set[str]) -> None:
    """Salience scores become claims so 'best moments' outlives the posts.

    The review UI (or a future Memories reel) can rank moments from these
    without re-running selection — the seam is the artifact + attribute
    claims, keyed by segment id and media span.
    """
    rows = []
    for packet in packets:
        if packet["album_id"] not in generated:
            continue
        for quote in packet["quotes"]:
            rows.append(
                {
                    "id": f"journal_salience_{quote['segment_id']}",
                    "album_id": packet["album_id"],
                    "segment_id": quote["segment_id"],
                    "source_video_id": quote["source_video_id"],
                    "start_s": quote["start_s"],
                    "end_s": quote["end_s"],
                    "speaker": quote.get("speaker") or "",
                    "text": quote["text"],
                    "salience": quote.get("salience"),
                    "reasons": quote.get("salience_reasons") or [],
                    "why": quote.get("why") or "",
                }
            )
    if not rows:
        return
    path = project / JOURNAL_SALIENCE_FILENAME
    existing = {row.get("id") for row in read_jsonl(path)}
    fresh = [row for row in rows if row["id"] not in existing]
    if not fresh:
        return
    writer = DualWriter.open(project, artifact=JOURNAL_SALIENCE_FILENAME, producer="journal/salience")
    for row in fresh:
        append_jsonl(path, row)
        writer.write_row(
            row,
            kind="attribute",
            media_id=row["source_video_id"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            confidence=None,
            assertion={
                "attribute": "memory_salience",
                "value": row["salience"],
                "reasons": row["reasons"],
                "segment_id": row["segment_id"],
                "album_id": row["album_id"],
            },
        )
    writer.close()


def _post_citations(post: dict[str, Any]) -> set[str]:
    return _blocks_citations(post.get("blocks") or [])


def _blocks_citations(blocks: list[dict[str, Any]]) -> set[str]:
    cited: set[str] = set()
    for block in blocks:
        cited.update(str(c) for c in block.get("citations") or [])
    return cited


def _valid_event_id(value: Any, packet: dict[str, Any]) -> str | None:
    event_id = str(value or "")
    return event_id if event_id in {event["id"] for event in packet["events"]} else None


def _default_kicker(packet: dict[str, Any]) -> str:
    kind = str(packet.get("album_type") or "")
    return {"school": "School days", "day": "One day", "travel": "Away", "home": "At home"}.get(kind, "From the tapes")


def _read_minutes(blocks: list[dict[str, Any]]) -> int:
    words = sum(len(str(block.get("text") or "").split()) for block in blocks if block.get("type") == "paragraph")
    return max(1, round(words / 200))


def _normalize_ws(text: str) -> str:
    return " ".join(str(text).split())


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Terra generator

JOURNAL_VOICE = """You are the family's journal keeper, writing one day's entry from digitized home
videos. Voice: warm, first-person-plural family voice ("we", "Filip", "Mama"), specific and concrete
— the delight must come from real observed detail (a verbatim thing someone said, a small moment),
never from invented color. No greeting-card sentiment. Short paragraphs. If a date or place is
uncertain, hedge honestly ("probably", "it looks like"). Russian quotes stay in Russian; add a short
translation in the block's "translation" field.

Write as remembered life, never as footage review: no "the video shows", "we began with", "the
footage", "the recording", "we see". Use people's names from the roster whenever the events or
quotes make clear who is who; when identity is genuinely unclear, prefer warm phrasing ("the
birthday boy", "one of the kids") over clinical phrasing ("the child", "an individual"). It is
always "his room", "our kitchen" — a journal writes from inside the family.

Pull-quotes are the heart of the entry. The quotes are ordered by memory value and the best carry a
"why" — the moment in which they were said. Choose quotes from the TOP of the list, and set each one
up with its moment in the sentence before it (use the "why", in your own words). A quote must land
as a memory: a first, a milestone, wonder, tenderness, a laugh. Never quote logistics; skip a quote
entirely rather than use a flat one."""

JOURNAL_RULES = """Write the post as JSON only, with this shape:
{"kicker": str (2-3 words), "title": str (short, warm, specific), "dek": str (one sentence),
 "hero_event_id": str (an event id), "blocks": [
   {"type": "heading", "text": str},
   {"type": "paragraph", "text": str, "citations": [event or segment ids], "entities": [{"span_text": str, "kind": "person|place|event", "id": str}]},
   {"type": "pullquote", "text": str (VERBATIM substring of a provided quote's text), "segment_id": str, "translation": str?},
   {"type": "clip", "event_id": str, "text": str (one-line caption), "citations": [event id]}
 ]}
HARD RULES — posts violating them are rejected mechanically:
1. Every paragraph carries "citations": ids ONLY from the provided events/quotes. No citation, no paragraph.
2. Pull-quote text must be a VERBATIM substring of the cited quote's text field. Do not fix grammar, do not translate in place.
3. Entity span_text must appear verbatim inside the block's text, and id must come from the provided people/places/events.
4. Only describe what the events/quotes support. Nothing invented.
5. 5-9 blocks total: open with a paragraph, include 1-3 pullquotes and 1-3 clips, close with a short paragraph."""


def _terra_generator(project: Path, deployment: str) -> Generator:
    from tapesplit.azure_openai_adapter import reasoning_chat_completion

    def generate(packet: dict[str, Any], feedback: list[str] | None) -> dict[str, Any]:
        packet_json = json.dumps(
            {key: packet[key] for key in ("title_hint", "date_label", "events", "quotes", "people", "places")},
            ensure_ascii=False,
        )
        messages = [
            {"role": "system", "content": JOURNAL_VOICE + "\n\n" + JOURNAL_RULES},
            {"role": "user", "content": f"Grounding packet for {packet['date_label']}:\n{packet_json}"},
        ]
        if feedback:
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous draft had rejected blocks — regenerate the full post fixing these:\n"
                    + "\n".join(feedback),
                }
            )
        result = reasoning_chat_completion(
            deployment=deployment,
            messages=messages,
            max_completion_tokens=4000,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            project_dir=project,
            operation="journal_generate",
        )
        content = result["choices"][0]["message"]["content"]
        post = json.loads(content)
        post["_generator"] = deployment
        return post

    return generate

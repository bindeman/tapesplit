"""Journal posts: grounding packets, memory salience, and the citation contract."""

import json
from pathlib import Path

from tapesplit.journal import (
    _child_speakers,
    _salience_heuristic,
    _segment_repeat_counts,
    _select_quotes,
    build_grounding_packets,
    generate_journal_posts,
    list_journal_posts,
)


def _write(project: Path, name: str, rows: list[dict]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / name).write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8"
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    _write(
        project,
        "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Morning at the lake",
                "summary": "Filip skips stones.",
                "metadata": {"source_ranges": [{"source_video_id": "video_000001", "start_s": 10.0, "end_s": 90.0}]},
                "confidence": 0.8,
            },
            {
                "id": "canonical_event_000002",
                "title": "Picnic",
                "summary": "Lunch on the grass.",
                "metadata": {"source_ranges": [{"source_video_id": "video_000001", "start_s": 120.0, "end_s": 200.0}]},
                "confidence": 0.7,
            },
        ],
    )
    _write(
        project,
        "albums.jsonl",
        [
            {
                "id": "album_000001",
                "title": "Lake day",
                "album_type": "day",
                "date_label": "JUN 1 2003",
                "canonical_event_ids": ["canonical_event_000001", "canonical_event_000002"],
                "cover_event_id": "canonical_event_000001",
                "confidence": 0.75,
            }
        ],
    )
    _write(
        project,
        "speaker_segments.jsonl",
        [
            {
                "id": "speaker_segment_000001",
                "source_video_id": "video_000001",
                "start_s": 15.0,
                "end_s": 19.0,
                "speaker_label": "Filip",
                "metadata": {"transcript_text": "Смотри, три раза прыгнул!"},
            },
            {
                "id": "speaker_segment_000002",
                "source_video_id": "video_000001",
                "start_s": 500.0,
                "end_s": 504.0,
                "speaker_label": "Ekaterina",
                "metadata": {"transcript_text": "Out of range chatter."},
            },
        ],
    )
    _write(
        project,
        "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filia / Filip / Филя",
                "aliases": ["Filip", "Филя"],
                "canonical_event_ids": ["canonical_event_000001"],
            }
        ],
    )
    _write(
        project,
        "event_place_roles.jsonl",
        [
            {
                "id": "event_place_role_000001",
                "canonical_event_id": "canonical_event_000001",
                "label": "the lake",
                "review_status": "unreviewed",
            }
        ],
    )
    return project


def _valid_post(packet: dict) -> dict:
    return {
        "kicker": "One day",
        "title": "Three skips at the lake",
        "dek": "Filip finally beats his record.",
        "hero_event_id": "canonical_event_000001",
        "blocks": [
            {
                "type": "paragraph",
                "text": "Filip spent the morning at the lake skipping stones.",
                "citations": ["canonical_event_000001"],
                "entities": [{"span_text": "Filip", "kind": "person", "id": "people_group_000001"}],
            },
            {"type": "pullquote", "text": "Смотри, три раза прыгнул!", "segment_id": "speaker_segment_000001"},
            {"type": "clip", "event_id": "canonical_event_000002", "text": "Lunch on the grass."},
        ],
    }


def test_salience_prefers_memories_over_logistics():
    first, first_reasons = _salience_heuristic(
        "Андрюша первый раз увидел снег!", named=True, child=False
    )
    blessing, _ = _salience_heuristic(
        "Сейчас будут благословлять еду, а потом пойдем на dinner.", named=True, child=False
    )
    procedural, proc_reasons = _salience_heuristic(
        "Передай хлеб и садись сюда.", named=True, child=False
    )
    assert first > blessing > procedural
    assert "first" in first_reasons
    assert "procedural" in proc_reasons
    # the exact v1 failure: the blessing quote must not outrank a first
    assert first - blessing >= 2.0


def test_select_quotes_orders_by_salience_and_drops_hallucination_loops():
    def seg(index: int, text: str, start: float = 10.0) -> dict:
        return {
            "id": f"speaker_segment_{index:06d}",
            "source_video_id": "video_000001",
            "start_s": start,
            "end_s": start + 3.0,
            "speaker_label": "Ekaterina",
            "metadata": {"transcript_text": text},
        }

    segments = {
        "video_000001": [
            seg(1, "Передай, пожалуйста, соль и хлеб."),
            seg(2, "Смотри, мама! Первый раз сам поехал!", start=30.0),
            *[seg(10 + i, "0:01, а в 5 минутах", start=50.0 + i) for i in range(4)],
        ]
    }
    ranges = {"video_000001": [(0.0, 100.0)]}
    quotes = _select_quotes(
        ranges, segments, repeat_counts=_segment_repeat_counts(segments)
    )
    assert quotes[0]["segment_id"] == "speaker_segment_000002"
    assert quotes[0]["salience"] > quotes[-1]["salience"]
    assert all("0:01" not in quote["text"] for quote in quotes)  # whisper loop never quoted
    assert "first" in quotes[0]["salience_reasons"]


def test_child_speakers_only_from_confirmed_age_models(tmp_path: Path):
    project = _project(tmp_path)
    _write(
        project,
        "person_age_models.jsonl",
        [
            {"person_group_id": "people_group_000001", "adult": False, "attribution_basis": "confirmed"},
            {"person_group_id": "people_group_000002", "adult": False, "attribution_basis": "candidate"},
        ],
    )
    people = [
        {"id": "people_group_000001", "label": "Filia / Filip", "aliases": ["Филя"]},
        {"id": "people_group_000002", "label": "Guest", "aliases": []},
    ]
    labels = _child_speakers(project, people)
    assert {"filia", "filip", "филя"} <= labels
    assert "guest" not in labels  # candidate-basis models are polluted; they don't vote


def test_ranker_attaches_why_reorders_and_persists_salience(tmp_path: Path):
    project = _project(tmp_path)
    seen: dict = {}

    def fake_ranker(packet, candidates):
        seen["candidate_ids"] = [c["segment_id"] for c in candidates]
        return {
            "speaker_segment_000001": "Filip finally lands the three-skip throw",
            "speaker_segment_999999": "unknown ids are dropped",
        }

    def fake_generator(packet, feedback):
        seen["top_quote"] = packet["quotes"][0]
        return _valid_post(packet)

    result = generate_journal_posts(project, generator=fake_generator, ranker=fake_ranker)
    assert result["posts_written"] == 1
    assert seen["top_quote"]["segment_id"] == "speaker_segment_000001"
    assert seen["top_quote"]["why"].startswith("Filip finally")

    rows = [json.loads(line) for line in (project / "journal_posts.jsonl").read_text().splitlines()]
    quote_block = next(block for block in rows[0]["blocks"] if block["type"] == "pullquote")
    assert quote_block["context"].startswith("Filip finally")  # why becomes the micro-context

    salience_rows = [
        json.loads(line) for line in (project / "journal_salience.jsonl").read_text().splitlines()
    ]
    assert salience_rows[0]["segment_id"] == "speaker_segment_000001"
    assert salience_rows[0]["why"].startswith("Filip finally")
    assert isinstance(salience_rows[0]["salience"], (int, float))


def test_packets_scope_quotes_to_event_ranges(tmp_path: Path):
    project = _project(tmp_path)
    packets = build_grounding_packets(project)
    assert len(packets) == 1
    packet = packets[0]
    assert [q["segment_id"] for q in packet["quotes"]] == ["speaker_segment_000001"]  # out-of-range one excluded
    assert packet["people"][0]["label"] in ("Filia", "Filip")  # shortest Latin alias wins
    assert packet["source_span"]["source_video_id"] == "video_000001"


def test_generate_enforces_grounding_contract(tmp_path: Path):
    project = _project(tmp_path)
    calls = []

    def fake_generator(packet, feedback):
        calls.append(feedback)
        post = _valid_post(packet)
        if feedback is None:
            post["blocks"].append(
                {"type": "paragraph", "text": "They saw a whale.", "citations": []}
            )
            post["blocks"].append(
                {"type": "pullquote", "text": "Look, it jumped four times!", "segment_id": "speaker_segment_000001"}
            )
        return post

    result = generate_journal_posts(project, generator=fake_generator)
    assert result["posts_written"] == 1
    assert calls[0] is None and calls[1] is not None  # regeneration round with feedback
    rows = [json.loads(l) for l in (project / "journal_posts.jsonl").read_text().splitlines()]
    post = rows[0]
    assert len(post["blocks"]) == 3  # uncited paragraph and paraphrased quote never survive
    quote = next(b for b in post["blocks"] if b["type"] == "pullquote")
    assert quote["speaker"] == "Filip" and quote["start_s"] == 15.0
    assert post["citations"] == sorted(
        {"canonical_event_000001", "canonical_event_000002", "speaker_segment_000001"}
    )
    assert post["generated"] is True


def test_generate_skips_existing_and_lists(tmp_path: Path):
    project = _project(tmp_path)
    result = generate_journal_posts(project, generator=lambda packet, feedback: _valid_post(packet))
    assert result["posts_written"] == 1
    again = generate_journal_posts(project, generator=lambda packet, feedback: _valid_post(packet))
    assert again["posts_written"] == 0
    assert again["skipped"][0]["reason"] == "post already exists"
    listed = list_journal_posts(project)
    assert listed[0]["id"] == "journal_post_album_000001"
    assert listed[0]["read_minutes"] >= 1


def test_entity_and_citation_id_validation(tmp_path: Path):
    project = _project(tmp_path)

    def bad_generator(packet, feedback):
        post = _valid_post(packet)
        post["blocks"] = [
            {
                "type": "paragraph",
                "text": "Grandpa waved.",
                "citations": ["canonical_event_999999"],
            },
            {
                "type": "paragraph",
                "text": "Filip laughed.",
                "citations": ["canonical_event_000001"],
                "entities": [{"span_text": "Grandpa", "kind": "person", "id": "people_group_000001"}],
            },
            _valid_post(packet)["blocks"][0],
        ]
        return post

    result = generate_journal_posts(project, generator=bad_generator)
    rows = [json.loads(l) for l in (project / "journal_posts.jsonl").read_text().splitlines()]
    assert result["rejected_blocks"] >= 2  # unknown citation id + entity span not in text
    assert len(rows[0]["blocks"]) == 1

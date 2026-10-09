import json
from pathlib import Path

from tapesplit.relationships import build_relationship_candidates
from tapesplit.storage import read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _event(event_id: str, start: float, end: float, *, people=(), event_type="home", video="video_000005") -> dict:
    return {
        "id": event_id,
        "title": event_id,
        "start_s": start,
        "end_s": end,
        "metadata": {
            "people": list(people),
            "event_type": event_type,
            "source_video_ids": [video],
            "source_ranges": [{"source_video_id": video, "start_s": start, "end_s": end}],
        },
    }


def _line(line_id: str, start: float, text: str, video="video_000005") -> dict:
    return {"id": line_id, "source_video_id": video, "start_s": start, "end_s": start + 2, "text": text, "language": "ru"}


def _project(tmp_path: Path, lines: list[dict], *, events=None, dates=None, people=()) -> list[dict]:
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {"id": "people_group_000001", "label": "Filip / Филипп", "aliases": ["Filip", "Филипп", "Филя"],
             "metadata": {"normalized_key": "filip"}},
            {"id": "people_group_000002", "label": "Grisha / Гриша", "aliases": ["Grisha", "Гриша"],
             "metadata": {"normalized_key": "grisha"}},
            {"id": "people_group_000003", "label": "Kim", "aliases": ["Kim", "Ким"],
             "metadata": {"normalized_key": "kim"}},
            *people,
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        events
        or [
            _event("canonical_event_000001", 0, 600, people=["Filip"]),
            _event("canonical_event_000002", 600, 1200, people=["Filip", "Grisha"], event_type="travel"),
        ],
    )
    _write_jsonl(tmp_path / "transcript_segments.jsonl", lines)
    _write_jsonl(tmp_path / "evidence.jsonl", [])
    _write_jsonl(tmp_path / "date_groups.jsonl", dates or [])
    _write_jsonl(
        tmp_path / "era_contexts.jsonl",
        [{"residence_label": "Madison, Wisconsin", "start_year": 2002, "end_year": 2004},
         {"residence_label": "Eugene, Oregon", "start_year": 2005, "end_year": 2009}],
    )
    build_relationship_candidates(tmp_path)
    return [row for row in read_jsonl(tmp_path / "relationship_candidates.jsonl") if row["source"] == "local_social_resolver"]


FAMILY = [_line("tr_000001", 10, "Дай папе йогурт, Филипп.")]  # makes Filip the child


def test_a_friend_word_with_a_name_makes_a_friend_candidate(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, "Гриша, друг Филиппа, пришел.")])

    assert [(row["subject_label"], row["predicate"], row["object_label"]) for row in social] == [
        ("Grisha / Гриша", "friend_or_classmate_candidate", "Filip / Филипп")
    ]


def test_other_is_not_friend(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, "Гриша, иди на другой стороне.")])

    assert social == []


def test_a_transcription_loop_counts_once(tmp_path: Path):
    loop = [_line(f"tr_{n:06d}", 100 + n, "Мальчик Гриша ходит с Филиппом в детский садик.") for n in range(2, 10)]
    social = _project(tmp_path, FAMILY + loop)

    assert len(social) == 1
    assert social[0]["predicate"] == "friend_or_classmate_candidate"
    assert social[0]["confidence"] == 0.72
    assert len(social[0]["metadata"]["transcript_segment_ids"]) == 1


def test_a_teacher_word_makes_a_teacher_candidate(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 120, "Он любит воспитательницу Ким.")])

    assert [(row["subject_label"], row["predicate"]) for row in social] == [("Kim", "teacher_or_caretaker_candidate")]


def test_an_uncle_name_is_a_relative_or_family_friend_question(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 130, "Подходи к дяде Оливье, подходи.")])

    assert [(row["subject_label"], row["predicate"]) for row in social] == [("Оливье", "honorific_family_friend_candidate")]
    assert read_jsonl(tmp_path / "relationship_review_tasks.jsonl")[-1]["question"] == "Is Оливье a relative or a family friend?"


def test_the_family_is_never_a_friend(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, "Филипп, друг, иди сюда.")])

    assert social == []


def test_scope_gives_the_years_and_where_they_happened(tmp_path: Path):
    events = [
        _event("canonical_event_000001", 0, 600, people=["Filip", "Grisha"]),
        _event("canonical_event_000002", 600, 1200, people=["Grisha"], event_type="travel"),
    ]
    dates = [
        {"canonical_event_ids": ["canonical_event_000001"], "date_value": "2004-12-05", "precision": "day"},
        {"canonical_event_ids": ["canonical_event_000002"], "date_value": "2006-03-20", "precision": "day"},
    ]
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, "Мальчик Гриша ходит с Филиппом в детский садик.")],
                      events=events, dates=dates)

    scope = social[0]["scope"]
    assert (scope["first_date"], scope["last_date"]) == ("2004-12-05", "2006-03-20")
    assert {item["label"] for item in scope["contexts"]} == {"Madison, Wisconsin", "a trip"}
    assert scope["met_on_trip"] is False


def test_a_line_between_moments_takes_the_nearest_moment(tmp_path: Path):
    events = [_event("canonical_event_000001", 0, 600, people=["Filip"]),
              _event("canonical_event_000002", 1500, 2000, people=["Filip"])]
    dates = [{"canonical_event_ids": ["canonical_event_000002"], "date_value": "2004-12-05", "precision": "day"}]
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 1300, "Мальчик Гриша ходит с Филиппом в детский садик.")],
                      events=events, dates=dates)

    assert [(row["subject_label"], row["scope"]["first_date"]) for row in social] == [("Grisha / Гриша", "2004-12-05")]


def test_a_name_far_from_the_friend_word_is_not_the_friend(tmp_path: Path):
    line = ("Вот этот человек, это мне про него друг рассказывал, что это конкретная личность, "
            "преподаватель в детской музыкальной школе. Зовут его Гриша.")
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, line)])

    assert social == []


def test_spellings_that_differ_by_an_ending_are_one_person(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, "Это вот Лена Ветрин, наша подруга."),
                                          _line("tr_000003", 700, "Это вот Лена Ветрина, наша подруга.")])

    assert [(row["subject_label"], row["predicate"], row["scope"]["moments"]) for row in social] == [
        ("Лена Ветрин", "family_friend_candidate", 2)
    ]


def test_the_known_name_wins_when_two_transcripts_disagree(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 300, "Подойди к дяде Грише."),
                                          _line("sp_000002", 302, "Подходи к дяде Оливье, подходи.")])

    assert [(row["subject_label"], row["predicate"]) for row in social] == [("Grisha / Гриша", "honorific_family_friend_candidate")]


def test_the_full_name_beats_the_first_name(tmp_path: Path):
    people = [{"id": "people_group_000004", "label": "Olga", "aliases": ["Olga", "Ольга"], "metadata": {"normalized_key": "olga"}},
              {"id": "people_group_000005", "label": "Olga Nikolaevna", "aliases": ["Olga Nikolaevna", "Ольга Николаевна"],
               "metadata": {"normalized_key": "olga nikolaevna"}}]
    social = _project(tmp_path, FAMILY + [_line("tr_000002", 100, "В школе осталась Ольга Николаевна, моя учительница физики.")],
                      people=people)

    assert [(row["subject_label"], row["predicate"], row["object_label"]) for row in social] == [
        ("Olga Nikolaevna", "teacher_or_caretaker_candidate", "the speaker")
    ]
    assert read_jsonl(tmp_path / "relationship_review_tasks.jsonl")[-1]["question"] == (
        "Is Olga Nikolaevna the speaker's teacher or caretaker?")


def test_a_misheard_surname_in_the_same_sentence_is_the_same_person(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [
        _line("tr_000002", 100, "Это вот Лена Ветрина, наша подруга. Она ожидает ребенка через месяц."),
        _line("tr_000003", 700, "Это Лена Ветрова, наша подруга. Она ожидает ребенка через месяц."),
        _line("tr_000004", 800, "Лена Ветрова, наша подруга, уехала в Москву."),
        _line("tr_000005", 900, "Вчера приходила Лена Петрова, наша подруга с работы."),
    ])

    assert [(row["subject_label"], row["scope"]["moments"]) for row in social] == [("Лена Ветрина", 2), ("Лена Петрова", 1)]
    assert len(social[0]["metadata"]["transcript_segment_ids"]) == 3


def test_a_line_that_continues_a_sentence_keeps_its_my(tmp_path: Path):
    social = _project(tmp_path, FAMILY + [
        _line("tr_000002", 100, "В школе осталась моя учительница физики,"),
        _line("tr_000003", 102.5, "а также директриса Лариса."),
    ])

    assert [(row["subject_label"], row["object_label"]) for row in social] == [("Лариса", "the speaker")]


def test_a_patronymic_alone_is_not_the_person(tmp_path: Path):
    people = [{"id": "people_group_000004", "label": "Olga", "aliases": ["Olga", "Ольга"], "metadata": {"normalized_key": "olga"}},
              {"id": "people_group_000005", "label": "Olga Nikolaevna", "aliases": ["Olga Nikolaevna", "Ольга Николаевна"],
               "metadata": {"normalized_key": "olga nikolaevna"}}]
    social = _project(tmp_path, FAMILY + [
        _line("tr_000002", 100, "В школе осталась Ольга Николаевна, моя учительница физики."),
        _line("tr_000003", 700, "Наталья Николаевна и Сергей."),
        _line("tr_000004", 900, "Ольга, иди сюда."),
    ], people=people)

    assert [(row["subject_label"], row["scope"]["moments"]) for row in social] == [("Olga Nikolaevna", 1)]

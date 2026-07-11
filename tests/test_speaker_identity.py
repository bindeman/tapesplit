import json
from pathlib import Path

from tapesplit.speaker_identity import build_speaker_identity_candidates
from tapesplit.storage import read_jsonl


def test_build_speaker_identity_candidates_uses_role_relationship_bridge(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Filip playing at home",
                "metadata": {
                    "people": ["Filip", "Mom"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 0, "end_s": 60}],
                },
            },
            {
                "id": "canonical_event_000002",
                "title": "Katya and Filip at the park",
                "metadata": {
                    "people": ["Katya", "Filip"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 100, "end_s": 150}],
                },
            },
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Filip",
                "aliases": ["Filip"],
                "kind": "person_candidate",
                "canonical_event_ids": ["canonical_event_000001", "canonical_event_000002"],
            },
            {
                "id": "people_group_000002",
                "label": "Ekaterina / Katya",
                "aliases": ["Ekaterina", "Katya"],
                "kind": "person_candidate",
                "canonical_event_ids": ["canonical_event_000002"],
            },
            {
                "id": "people_group_000003",
                "label": "Mom (Filip playing at home)",
                "aliases": ["Mom"],
                "kind": "role_candidate",
                "confidence": 0.75,
                "canonical_event_ids": ["canonical_event_000001"],
            },
        ],
    )
    _write_jsonl(
        tmp_path / "relationship_candidates.jsonl",
        [
            {
                "id": "relationship_candidate_000001",
                "subject_entity_id": "people_group_000002",
                "subject_label": "Ekaterina / Katya",
                "predicate": "mother_candidate",
                "object_entity_id": "people_group_000001",
                "object_label": "Filip",
                "confidence": 0.84,
                "scope": {"canonical_event_ids": ["canonical_event_000002"], "source_video_ids": ["video_000001"], "start_s": 100, "end_s": 150},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 10,
                "text": "Filip, come here",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "speaker_segments.jsonl",
        [
            {
                "id": "speaker_segment_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 30,
                "speaker_label": "LOCAL_SPEAKER_00",
                "metadata": {"transcript_segment_id": "tr_000001"},
            }
        ],
    )

    result = build_speaker_identity_candidates(tmp_path)
    rows = read_jsonl(tmp_path / "speaker_identity_candidates.jsonl")

    assert result["speaker_tracks"] == 1
    assert any(row["person_group_id"] == "people_group_000002" for row in rows)
    katya = next(row for row in rows if row["person_group_id"] == "people_group_000002")
    assert "relationship_candidate_000001" in katya["relationship_candidate_ids"]
    assert "role_identity_bridge" in katya["metadata"]["signal_sources"]


def test_build_speaker_identity_candidates_detects_self_identification(tmp_path: Path):
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Narration",
                "metadata": {"people": ["Ekaterina"], "source_ranges": [{"source_video_id": "video_000001", "start_s": 0, "end_s": 20}]},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Ekaterina",
                "aliases": ["Katya"],
                "kind": "person_candidate",
                "canonical_event_ids": ["canonical_event_000001"],
            }
        ],
    )
    _write_jsonl(tmp_path / "relationship_candidates.jsonl", [])
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 6,
                "text": "Hi, this is Ekaterina filming at home",
            }
        ],
    )
    _write_jsonl(
        tmp_path / "speaker_segments.jsonl",
        [
            {
                "id": "speaker_segment_000001",
                "source_video_id": "video_000001",
                "start_s": 0,
                "end_s": 6,
                "speaker_label": "LOCAL_SPEAKER_00",
                "metadata": {"transcript_segment_id": "tr_000001"},
            }
        ],
    )

    build_speaker_identity_candidates(tmp_path)
    rows = read_jsonl(tmp_path / "speaker_identity_candidates.jsonl")

    assert rows[0]["person_group_id"] == "people_group_000001"
    assert rows[0]["confidence"] >= 0.7
    assert "self_identification_phrase" in rows[0]["metadata"]["signal_sources"]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def test_voice_bucket_analysis_scopes_and_classifies(tmp_path: Path):
    """Anonymous buckets scope per tape; analysis verdicts become signals/flags."""
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Home",
                "metadata": {
                    "people": ["Ekaterina"],
                    "source_ranges": [{"source_video_id": "video_000001", "start_s": 0, "end_s": 200}],
                },
            }
        ],
    )
    _write_jsonl(
        tmp_path / "people_groups.jsonl",
        [
            {
                "id": "people_group_000001",
                "label": "Ekaterina / Katya",
                "aliases": ["Ekaterina", "Katya"],
                "kind": "person_candidate",
                "canonical_event_ids": ["canonical_event_000001"],
            },
            {
                "id": "people_group_000002",
                "label": "Viktor Sokolov",
                "aliases": ["Viktor Sokolov"],
                "kind": "person_candidate",
                "canonical_event_ids": [],
            },
        ],
    )
    _write_jsonl(tmp_path / "transcript_segments.jsonl", [])
    _write_jsonl(tmp_path / "relationship_candidates.jsonl", [])
    _write_jsonl(tmp_path / "face_identity_candidates.jsonl", [])
    _write_jsonl(tmp_path / "face_clusters.jsonl", [])
    _write_jsonl(
        tmp_path / "speaker_segments.jsonl",
        [
            {"id": "speaker_segment_000001", "speaker_label": "AZ_SPEAKER_00",
             "source_video_id": "video_000001", "start_s": 5.0, "end_s": 12.0},
            {"id": "speaker_segment_000002", "speaker_label": "AZ_SPEAKER_00",
             "source_video_id": "video_000002", "start_s": 5.0, "end_s": 12.0},
            {"id": "speaker_segment_000003", "speaker_label": "AZ_SPEAKER_01",
             "source_video_id": "video_000003", "start_s": 5.0, "end_s": 12.0},
        ],
    )
    _write_jsonl(
        tmp_path / "voice_bucket_analysis.jsonl",
        [
            {"id": "voice_bucket_video_000001_AZ_SPEAKER_00", "tape": "video_000001",
             "speaker_label": "AZ_SPEAKER_00", "verdict": "named_overflow",
             "match_label": "Ekaterina", "match_cosine": 0.91, "second_cosine": 0.7,
             "ambiguous": False, "onscreen_fraction": 0.3, "directive_rate": 0.02,
             "segment_count": 1, "duration_min": 0.1},
            {"id": "voice_bucket_video_000002_AZ_SPEAKER_00", "tape": "video_000002",
             "speaker_label": "AZ_SPEAKER_00", "verdict": "broadcast",
             "match_label": None, "match_cosine": 0.1, "second_cosine": None,
             "ambiguous": False, "onscreen_fraction": 0.0, "directive_rate": 0.0,
             "segment_count": 1, "duration_min": 0.1},
            {"id": "voice_bucket_video_000003_AZ_SPEAKER_01", "tape": "video_000003",
             "speaker_label": "AZ_SPEAKER_01", "verdict": "recurring_adult",
             "match_label": None, "match_cosine": 0.2, "second_cosine": None,
             "ambiguous": False, "onscreen_fraction": 0.1, "directive_rate": 0.08,
             "recurring_cluster_mean_cosine": 0.65,
             "segment_count": 1, "duration_min": 0.1},
        ],
    )

    result = build_speaker_identity_candidates(tmp_path, min_confidence=0.2)
    rows = read_jsonl(tmp_path / "speaker_identity_candidates.jsonl")

    by_speaker = {row["speaker_label"]: row for row in rows}
    # anonymous labels are tape-scoped
    assert "AZ_SPEAKER_00@video_000001" in by_speaker
    overflow = by_speaker["AZ_SPEAKER_00@video_000001"]
    assert overflow["person_group_id"] == "people_group_000001"
    assert any("voiceprint" in str(signal) for signal in overflow.get("supporting_signals", []))
    # recurring adult binds as a low-confidence hypothesis, never auto-strength
    recurring = by_speaker.get("AZ_SPEAKER_01@video_000003")
    assert recurring is not None and recurring["person_group_id"] == "people_group_000002"
    assert recurring["confidence"] < 0.7
    # broadcast bucket emits no identity candidate, only a flag
    assert "AZ_SPEAKER_00@video_000002" not in by_speaker
    flags = read_jsonl(tmp_path / "verification_flags.jsonl")
    assert {f["target_id"] for f in flags} == {
        "AZ_SPEAKER_00@video_000002",
        "AZ_SPEAKER_01@video_000003",
    }
    # idempotent flag append
    build_speaker_identity_candidates(tmp_path, min_confidence=0.2)
    assert len(read_jsonl(tmp_path / "verification_flags.jsonl")) == len(flags)
    assert result["voice_bucket_rows"] == 3

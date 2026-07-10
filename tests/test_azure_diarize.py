"""Azure OpenAI cloud diarization backend: planning, reconciliation, labels."""

import json
from pathlib import Path

import pytest

from tapesplit.azure_openai_adapter import _encode_multipart
from tapesplit.speakers import (
    AZURE_OPENAI_DIARIZE_BACKEND,
    _mine_speaker_reference_clips,
    _plan_audio_parts,
    _reconcile_azure_parts,
    _reference_display_name,
    _unify_unnamed_azure_labels,
    diarize_project_speakers,
)
from tapesplit.storage import read_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_plan_audio_parts_short_audio_is_single_part():
    assert _plan_audio_parts(90.0) == [(0.0, 90.0)]


def test_plan_audio_parts_overlap_and_coverage():
    parts = _plan_audio_parts(2500.0, part_s=1200.0, overlap_s=30.0)

    assert parts[0] == (0.0, 1200.0)
    assert parts[1] == (1170.0, 2370.0)
    assert parts[2] == (2340.0, 2500.0)
    # consecutive parts overlap by exactly overlap_s until the tail
    assert parts[0][1] - parts[1][0] == 30.0
    assert parts[-1][1] == 2500.0


def test_reconcile_azure_parts_drops_overlap_duplicates():
    parts = [(0.0, 1200.0), (1170.0, 2340.0)]
    duplicate = {"part_index": 0, "start_s": 1180.0, "end_s": 1190.0, "raw_speaker": "A", "text": "x"}
    duplicate_again = {"part_index": 1, "start_s": 1180.2, "end_s": 1190.1, "raw_speaker": "B", "text": "x"}
    early = {"part_index": 0, "start_s": 5.0, "end_s": 9.0, "raw_speaker": "A", "text": "hi"}
    late = {"part_index": 1, "start_s": 2000.0, "end_s": 2004.0, "raw_speaker": "B", "text": "bye"}

    kept = _reconcile_azure_parts([[early, duplicate], [duplicate_again, late]], parts)

    # overlap zone midpoint is 1185: first part owns [0, 1185), second [1185, ...)
    kept_keys = {(row["part_index"], row["start_s"]) for row in kept}
    assert (0, 5.0) in kept_keys
    assert (1, 2000.0) in kept_keys
    # exactly one copy of the seam segment survives
    seam = [row for row in kept if 1170.0 <= row["start_s"] <= 1200.0]
    assert len(seam) == 1


def test_reconcile_azure_parts_single_part_keeps_everything():
    rows = [
        {"part_index": 0, "start_s": 1.0, "end_s": 2.0, "raw_speaker": "A", "text": "a"},
        {"part_index": 0, "start_s": 3.0, "end_s": 4.0, "raw_speaker": "B", "text": "b"},
    ]
    assert len(_reconcile_azure_parts([rows], [(0.0, 100.0)])) == 2


def test_unify_labels_known_names_pass_through_and_clusters_merge_parts():
    rows = [
        {"part_index": 0, "start_s": 1.0, "end_s": 4.0, "raw_speaker": "Ekaterina", "text": ""},
        {"part_index": 0, "start_s": 10.0, "end_s": 14.0, "raw_speaker": "A", "text": ""},
        {"part_index": 1, "start_s": 1200.0, "end_s": 1204.0, "raw_speaker": "B", "text": ""},
        {"part_index": 1, "start_s": 1300.0, "end_s": 1304.0, "raw_speaker": "Ekaterina", "text": ""},
    ]

    def embed_fn(windows):
        # every unnamed group embeds to the same voice → one cluster
        return [[1.0, 0.0, 0.0] for _ in windows]

    mapping = _unify_unnamed_azure_labels(rows, known_names={"Ekaterina"}, embed_fn=embed_fn)

    assert mapping[(0, "Ekaterina")] == "Ekaterina"
    assert mapping[(1, "Ekaterina")] == "Ekaterina"
    assert mapping[(0, "A")] == mapping[(1, "B")]
    assert mapping[(0, "A")].startswith("AZ_SPEAKER_")


def test_unify_labels_embedding_failure_keeps_part_scoped_labels():
    rows = [
        {"part_index": 0, "start_s": 10.0, "end_s": 14.0, "raw_speaker": "A", "text": ""},
        {"part_index": 1, "start_s": 1200.0, "end_s": 1204.0, "raw_speaker": "A", "text": ""},
    ]

    mapping = _unify_unnamed_azure_labels(rows, known_names=set(), embed_fn=lambda windows: None)

    assert mapping[(0, "A")] == "AZ_P00_A"
    assert mapping[(1, "A")] == "AZ_P01_A"
    assert mapping[(0, "A")] != mapping[(1, "A")]


def test_unify_labels_single_part_needs_no_embeddings():
    rows = [
        {"part_index": 0, "start_s": 10.0, "end_s": 14.0, "raw_speaker": "A", "text": ""},
        {"part_index": 0, "start_s": 20.0, "end_s": 24.0, "raw_speaker": "B", "text": ""},
    ]

    def embed_fn(windows):  # pragma: no cover - must not be called
        raise AssertionError("single-part tapes must not embed")

    mapping = _unify_unnamed_azure_labels(rows, known_names=set(), embed_fn=embed_fn)

    assert mapping[(0, "A")] == "AZ_SPEAKER_00"
    assert mapping[(0, "B")] == "AZ_SPEAKER_01"


def test_reference_display_name_prefers_latin_alias():
    assert _reference_display_name("Филип / Filip / Filya") == "Filip"
    assert _reference_display_name("Ekaterina / Katya") == "Ekaterina"


def test_mine_speaker_reference_clips_dedupes_and_caps(tmp_path: Path, monkeypatch):
    project = tmp_path
    (project / "audio").mkdir()
    for video in ("video_000001",):
        (project / "audio" / f"{video}.wav").write_bytes(b"fake")
    _write_jsonl(
        project / "people_groups.jsonl",
        [
            {"id": "pg_1", "label": "Ekaterina / Katya"},
            {"id": "pg_2", "label": "Filip / Filya"},
            {"id": "pg_3", "label": "Filip / Philip"},  # duplicate person, other group
        ],
    )
    segments = []
    for index, (pid, conf, dur) in enumerate(
        [("pg_1", 0.9, 5.0), ("pg_1", 0.8, 4.0), ("pg_2", 0.85, 6.0), ("pg_3", 0.95, 5.0)]
    ):
        segments.append(
            {
                "source_video_id": "video_000001",
                "start_s": 10.0 * index,
                "end_s": 10.0 * index + dur,
                "speaker_label": f"LOCAL_SPEAKER_{index:02d}",
                "confidence": conf,
                "person_group_id": pid,
            }
        )
    _write_jsonl(project / "speaker_segments.jsonl", segments)

    extracted = []

    def fake_clip(audio_path, start_s, duration_s, output):
        extracted.append((start_s, duration_s))
        Path(output).write_bytes(b"mp3")

    monkeypatch.setattr("tapesplit.speakers._extract_audio_clip_mp3", fake_clip)

    clips = _mine_speaker_reference_clips(project)

    # two distinct names despite three groups (both Filip groups collapse)
    assert set(clips) == {"Ekaterina", "Filip"}
    assert all(Path(path).exists() for path in clips.values())
    # manifest caches the result — a second call must not re-extract
    extracted.clear()
    again = _mine_speaker_reference_clips(project)
    assert again == clips
    assert extracted == []


def test_azure_backend_requires_configuration(tmp_path: Path, monkeypatch):
    # the repo .env re-populates Azure vars via load_dotenv, so stub the
    # config loader instead of clearing the process environment
    from tapesplit.azure_openai_adapter import AzureOpenAIConfig

    empty = AzureOpenAIConfig(
        endpoint=None,
        api_key=None,
        region=None,
        api_version=None,
        reasoning_deployment=None,
        fast_deployment=None,
        full_deployment=None,
        chat_deployment=None,
        codex_deployment=None,
        whisper_deployment=None,
    )
    monkeypatch.setattr(
        "tapesplit.azure_openai_adapter.load_azure_openai_config", lambda env_path=None: empty
    )
    _write_jsonl(tmp_path / "tapes.jsonl", [{"id": "video_000001", "path": "video.mp4"}])

    with pytest.raises(RuntimeError, match="azure-openai diarization needs"):
        diarize_project_speakers(tmp_path, backend=AZURE_OPENAI_DIARIZE_BACKEND)


def test_azure_backend_end_to_end_with_fake_service(tmp_path: Path, monkeypatch):
    project = tmp_path
    (project / "audio").mkdir()
    wav = project / "audio" / "video_000001.wav"
    wav.write_bytes(b"fake-wav")
    _write_jsonl(project / "tapes.jsonl", [{"id": "video_000001", "path": "video.mp4"}])

    monkeypatch.setenv("AZURE_OPENAI_API_BASE", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

    monkeypatch.setattr(
        "tapesplit.speakers.extract_project_audio",
        lambda project_dir, source_video_id=None, force=False: {
            "audio_files": [{"source_video_id": "video_000001", "audio_path": str(wav)}]
        },
    )
    monkeypatch.setattr("tapesplit.speakers._audio_duration_s", lambda path: 2500.0)
    monkeypatch.setattr("tapesplit.speakers._mine_speaker_reference_clips", lambda project: {})

    part_files = []

    def fake_part(audio_path, start_s, duration_s):
        part = tmp_path / f"part_{start_s:.0f}.mp3"
        part.write_bytes(b"mp3")
        part_files.append((start_s, duration_s))
        return part

    monkeypatch.setattr("tapesplit.speakers._extract_audio_part_mp3", fake_part)

    calls = []

    def fake_transcribe(*, audio_path, deployment, known_speakers, project_dir):
        calls.append(deployment)
        part_index = len(calls) - 1
        # rel 100s sits in each part's own (non-overlap) core zone
        return {
            "text": "hello",
            "segments": [
                {"id": part_index, "speaker": "A", "start": 100.0, "end": 104.0, "text": f"part{part_index}"}
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    monkeypatch.setattr("tapesplit.azure_openai_adapter.transcribe_diarize", fake_transcribe)
    monkeypatch.setattr(
        "tapesplit.speakers._azure_segment_embed_fn",
        lambda project, source_video_id, audio_path: (lambda windows: [[1.0, 0.0] for _ in windows]),
    )

    result = diarize_project_speakers(project, backend=AZURE_OPENAI_DIARIZE_BACKEND, force=True)

    assert result["backend"] == AZURE_OPENAI_DIARIZE_BACKEND
    assert len(calls) == 3  # 2500s → 3 parts
    rows = read_jsonl(project / "speaker_segments.jsonl")
    assert len(rows) == 3
    # part-relative 100.0 offsets to tape-absolute per part start
    assert [row["start_s"] for row in rows] == [100.0, 1270.0, 2440.0]
    assert all(row["provider"] == "azure_openai" for row in rows)
    # same voice across parts unified to one stable label
    assert len({row["speaker_label"] for row in rows}) == 1
    runs = read_jsonl(project / "speaker_runs.jsonl")
    assert runs[-1]["provider"] == "azure_openai"
    assert runs[-1]["metadata"]["parts_by_source"] == {"video_000001": 3}


def test_encode_multipart_contains_fields_and_file():
    body, content_type = _encode_multipart(
        [("response_format", "diarized_json"), ("known_speaker_names[]", "Ekaterina")],
        files=[("file", "part.mp3", b"\x00\x01audio", "audio/mpeg")],
    )

    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]
    assert body.count(f"--{boundary}".encode()) == 4  # 2 fields + 1 file + terminator
    assert b'name="response_format"\r\n\r\ndiarized_json' in body
    assert b'name="known_speaker_names[]"\r\n\r\nEkaterina' in body
    assert b'filename="part.mp3"' in body
    assert b"Content-Type: audio/mpeg" in body
    assert b"\x00\x01audio" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())

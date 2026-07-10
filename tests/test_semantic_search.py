"""Semantic search: incremental dense upgrade, sectioned query, CLIP layer, sidecar."""

import io
import json
import sqlite3
from pathlib import Path

import pytest

from tapesplit.auto import AutoOptions, StageContext, StageSkipped, _run_semantic_embed, stage_names
from tapesplit.search import SEARCH_DB_NAME, build_search_index
from tapesplit.semantic_search import (
    SEMANTIC_CACHE_DB_NAME,
    handle_semantic_request,
    semantic_query,
    serve_semantic_search,
    upgrade_search_index_semantic,
)
from tapesplit.storage import append_jsonl


AXES = ["school", "birthday", "beach", "snow"]


def _axis_vector(text: str) -> list[float]:
    lowered = text.lower()
    vector = [0.0] * len(AXES)
    for index, axis in enumerate(AXES):
        if axis in lowered or (axis == "school" and "школ" in lowered):
            vector[index] = 1.0
    if not any(vector):
        vector[-1] = 0.01  # keep non-zero so normalization is stable
    return vector


class CountingEncoder:
    def __init__(self):
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        return [_axis_vector(text) for text in texts]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    append_jsonl(
        project / "transcript_segments.jsonl",
        {
            "id": "ts_1",
            "source_video_id": "video_000001",
            "start_s": 10.0,
            "end_s": 14.0,
            "text": "Сегодня первое сентября, идём в школу.",
            "language": "ru",
        },
    )
    append_jsonl(
        project / "transcript_segments.jsonl",
        {
            "id": "ts_2",
            "source_video_id": "video_000001",
            "start_s": 100.0,
            "end_s": 104.0,
            "text": "Happy birthday to you!",
            "language": "en",
        },
    )
    append_jsonl(
        project / "canonical_events.jsonl",
        {
            "id": "canonical_event_000001",
            "title": "First Day of School",
            "summary": "Walking to school in the morning.",
            "start_s": 5.0,
            "end_s": 60.0,
        },
    )
    append_jsonl(
        project / "people_groups.jsonl",
        {"id": "pg_1", "label": "Ekaterina", "aliases": ["Katya", "Екатерина"]},
    )
    append_jsonl(
        project / "place_groups.jsonl",
        {"id": "plg_1", "label": "Maplewood Learning Community", "place_type": "school"},
    )
    append_jsonl(
        project / "visual_assets.jsonl",
        {
            "id": "va_1",
            "subject_type": "scene",
            "subject_id": "scene_1",
            "source_video_id": "video_000001",
            "time_s": 12.0,
            "thumbnail_path": "keyframes/scenes/va_1_thumb.jpg",
            "keyframe_path": "keyframes/scenes/va_1.jpg",
        },
    )
    append_jsonl(
        project / "visual_embeddings.jsonl",
        {
            "id": "visual_embedding_000001",
            "visual_asset_id": "va_1",
            "source_video_id": "video_000001",
            "source_subject_type": "scene",
            "source_subject_id": "scene_1",
            "time_s": 12.0,
            "start_s": 10.0,
            "end_s": 15.0,
            "dim": len(AXES),
            "vector": _axis_vector("snow"),
        },
    )
    build_search_index(project)
    return project


def test_upgrade_embeds_whitelisted_docs_and_caches(tmp_path):
    project = _project(tmp_path)
    encoder = CountingEncoder()

    first = upgrade_search_index_semantic(project, text_encoder=encoder)
    assert first["documents"] > 0
    assert first["embedded"] == first["documents"]
    assert first["clip_vectors"] == 1
    assert (project / SEMANTIC_CACHE_DB_NAME).exists()
    first_calls = encoder.calls
    assert first_calls >= first["documents"]

    conn = sqlite3.connect(project / SEARCH_DB_NAME)
    conn.row_factory = sqlite3.Row
    dense_types = {
        row["record_type"]
        for row in conn.execute(
            "SELECT d.record_type FROM dense_vectors v JOIN documents d ON d.id = v.document_id"
        )
    }
    conn.close()
    assert "transcript" in dense_types
    assert "event" in dense_types
    assert "people_group" in dense_types
    # Graph plumbing stays sparse-only.
    assert "visual_similarity_edge" not in dense_types

    # Second run: everything served from the content-hash cache.
    second = upgrade_search_index_semantic(project, text_encoder=encoder)
    assert second["embedded"] == 0
    assert second["cached_hits"] == second["documents"]
    assert encoder.calls == first_calls


def test_semantic_query_sections_and_clip_layer(tmp_path):
    project = _project(tmp_path)
    upgrade_search_index_semantic(project, text_encoder=CountingEncoder())

    result = semantic_query(
        project,
        "school",
        text_encoder=CountingEncoder(),
        clip_encoder=CountingEncoder(),
    )
    assert result["semantic"] is True
    sections = result["sections"]
    assert set(sections) == {"people", "places", "moments", "spoken", "seen"}
    assert any(hit["source_id"] == "ts_1" for hit in sections["spoken"])
    assert any(hit["source_id"] == "canonical_event_000001" for hit in sections["moments"])
    assert any(hit["source_id"] == "plg_1" for hit in sections["places"])
    # The Russian school transcript matches via the (fake) multilingual vector,
    # not via keyword overlap — the token "school" never appears in it.
    school_hit = next(hit for hit in sections["spoken"] if hit["source_id"] == "ts_1")
    assert school_hit["score"] > 0

    snow = semantic_query(
        project,
        "snow",
        text_encoder=CountingEncoder(),
        clip_encoder=CountingEncoder(),
    )
    seen = snow["sections"]["seen"]
    assert seen and seen[0]["kind"] == "keyframe"
    assert seen[0]["thumbnail_path"] == "keyframes/scenes/va_1_thumb.jpg"
    assert seen[0]["time_s"] == 12.0

    # A query orthogonal to the scene vector stays below the CLIP floor.
    beach = semantic_query(
        project,
        "beach",
        text_encoder=CountingEncoder(),
        clip_encoder=CountingEncoder(),
    )
    assert not [hit for hit in beach["sections"]["seen"] if hit["kind"] == "keyframe"]


def test_query_without_semantic_upgrade_still_answers(tmp_path):
    project = _project(tmp_path)
    result = semantic_query(project, "birthday")
    assert result["semantic"] is False
    assert any(hit["source_id"] == "ts_2" for hit in result["sections"]["spoken"])


def test_sidecar_protocol(tmp_path):
    project = _project(tmp_path)
    encoder = CountingEncoder()

    pong = handle_semantic_request({"id": 7, "op": "ping"}, project, text_encoder=encoder, clip_encoder=encoder)
    assert pong == {"id": 7, "ok": True, "pong": True}

    response = handle_semantic_request(
        {"id": 8, "op": "query", "q": "birthday", "limit": 5},
        project,
        text_encoder=encoder,
        clip_encoder=encoder,
    )
    assert response["ok"] is True and response["id"] == 8
    assert any(hit["source_id"] == "ts_2" for hit in response["result"]["sections"]["spoken"])

    unknown = handle_semantic_request({"id": 9, "op": "explode"}, project, text_encoder=encoder, clip_encoder=encoder)
    assert unknown["ok"] is False

    # Bad queries never kill the loop.
    broken = handle_semantic_request({"id": 10, "op": "query", "q": "x", "limit": "not-a-number"}, project, text_encoder=encoder, clip_encoder=encoder)
    assert broken["ok"] is False


def test_serve_loop_framing(tmp_path):
    project = _project(tmp_path)  # sparse-only: serve loads no models
    stdin = io.StringIO(json.dumps({"id": 1, "op": "ping"}) + "\nnot-json\n" + json.dumps({"id": 2, "op": "query", "q": "birthday"}) + "\n")
    stdout = io.StringIO()
    serve_semantic_search(project, stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["ready"] is True and lines[0]["semantic"] is False
    assert lines[1] == {"id": 1, "ok": True, "pong": True}
    assert lines[2]["ok"] is False  # bad json reported, loop survived
    assert lines[3]["id"] == 2 and lines[3]["ok"] is True


def test_stage_registered_and_skips_without_index(tmp_path):
    assert "semantic-embed" in stage_names()
    project = tmp_path / "empty.tapesplit"
    project.mkdir()
    context = StageContext(project=project, options=AutoOptions(), capabilities={})
    with pytest.raises(StageSkipped):
        _run_semantic_embed(context)

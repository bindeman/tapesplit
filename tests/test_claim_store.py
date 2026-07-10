"""v2 M1 claim substrate: span validation, dual-write parity, oracle diff."""

import json
from pathlib import Path

import pytest

from tapesplit.claim_store import (
    ClaimStore,
    Span,
    SpanValidationError,
    backfill_project_claims,
    chunk_span_to_source,
    diff_v1_artifact,
    validate_span,
)
from tapesplit.gemini_import import import_gemini_analysis
from tapesplit.heuristic_events import build_heuristic_events
from tapesplit.media import build_media_index, source_media_id
from tapesplit.review_actions import apply_review_actions
from tapesplit.storage import append_jsonl, read_jsonl


def _chunk_project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir(parents=True)
    (project / "tapes.jsonl").write_text(
        '{"id":"video_000001","filename":"tape1.mp4","path":"/v/tape1.mp4","probe":{"duration_s":7095}}\n',
        encoding="utf-8",
    )
    (project / "gemini_analyses.jsonl").write_text(
        (
            '{"analysis_run_id":"run_a","source_video_id":"video_000001",'
            '"chunk_id":"chunk_0008","chunk_index":8,"chunk_start_s":6195,'
            '"chunk_end_s":7095,"chunk_duration_s":900,"time_basis":"chunk",'
            '"analysis":{"event_candidates":['
            '{"title":"Lab tour","start_s":100,"end_s":300,"summary":"geology lab","confidence":0.8},'
            '{"title":"Skiing","start_s":700,"end_s":890,"summary":"ski hill","confidence":0.7}]}}\n'
        ),
        encoding="utf-8",
    )
    return project


# --- span validation: reject, never clamp ---


def test_chunk_span_exceeding_chunk_duration_is_rejected():
    with pytest.raises(SpanValidationError):
        validate_span(Span("chunk", 848.0, 1199.0), chunk_duration_s=900.0)


def test_chunk_span_requires_chunk_duration():
    with pytest.raises(SpanValidationError):
        validate_span(Span("chunk", 0.0, 10.0))


def test_span_end_before_start_is_rejected():
    with pytest.raises(SpanValidationError):
        validate_span(Span("source", 20.0, 10.0))


def test_unknown_clock_is_rejected():
    with pytest.raises(SpanValidationError):
        validate_span(Span("tape", 0.0, 10.0))


def test_source_span_exceeding_media_duration_is_rejected():
    with pytest.raises(SpanValidationError):
        validate_span(Span("source", 90.0, 130.0), media_duration_s=100.0)


def test_chunk_to_source_conversion_is_explicit_and_records_offset():
    converted, applied = chunk_span_to_source(
        Span("chunk", 100.0, 300.0), chunk_offset_s=6195.0, chunk_duration_s=900.0
    )
    assert (converted.clock, converted.start_s, converted.end_s) == ("source", 6295.0, 6495.0)
    assert applied == 6195.0


def test_store_rejects_invalid_span_at_write_time(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    with ClaimStore(project) as store:
        with pytest.raises(SpanValidationError):
            store.add_claim(
                "event",
                producer="test",
                span=Span("chunk", 848.0, 1199.0),
                chunk_duration_s=900.0,
            )
        assert store.stats()["claims_total"] == 0


# --- dual-write parity and oracle diff ---


def test_gemini_import_dual_writes_claims_byte_identical(tmp_path: Path):
    project = _chunk_project(tmp_path)
    result = import_gemini_analysis(project)
    assert result["claims_dual_write"]["enabled"] is True
    assert result["claims_dual_write"]["claims_written"] == result["events"] == 2

    diff = diff_v1_artifact(project, "gemini_events.jsonl")
    assert diff["byte_identical"] is True
    assert diff["v1_rows"] == 2

    with ClaimStore(project) as store:
        claims = store.claims(v1_artifact="gemini_events.jsonl")
    assert all(claim["producer"] == "gemini_vertex" for claim in claims)
    assert all(claim["run_id"] == "run_a" for claim in claims)
    assert all(claim["chunk_id"] == "chunk_0008" for claim in claims)
    assert all(claim["clock"] == "source" for claim in claims)
    # chunk offset applied: first event 100-300 chunk-relative -> 6295-6495 source.
    assert claims[0]["start_s"] == 6295.0
    scope = claims[0]["payload"]["provenance"]["request_scope"]
    assert scope["kind"] == "chunk" and scope["chunk_offset_s"] == 6195


def test_disabled_substrate_leaves_v1_output_byte_identical(tmp_path: Path, monkeypatch):
    enabled = _chunk_project(tmp_path / "enabled")
    import_gemini_analysis(enabled)

    monkeypatch.setenv("TAPESPLIT_CLAIMS", "0")
    disabled = _chunk_project(tmp_path / "disabled")
    result = import_gemini_analysis(disabled)
    assert result["claims_dual_write"] == {"enabled": False}
    assert not (disabled / "claim_store.sqlite3").exists()
    assert (disabled / "gemini_events.jsonl").read_bytes() == (enabled / "gemini_events.jsonl").read_bytes()


def test_reimport_supersedes_old_claims_and_diff_stays_clean(tmp_path: Path):
    project = _chunk_project(tmp_path)
    import_gemini_analysis(project)
    import_gemini_analysis(project)

    diff = diff_v1_artifact(project, "gemini_events.jsonl")
    assert diff["byte_identical"] is True
    with ClaimStore(project) as store:
        stats = store.stats()
    assert stats["claims_live"] == 2
    assert stats["claims_superseded"] == 2  # nothing deleted, only superseded


def test_skipped_event_candidates_become_rejection_claims(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    (project / "tapes.jsonl").write_text(
        '{"id":"video_000001","probe":{"duration_s":100}}\n', encoding="utf-8"
    )
    (project / "gemini_analyses.jsonl").write_text(
        (
            '{"analysis_run_id":"run_a","source_video_id":"video_000001",'
            '"analysis":{"event_candidates":['
            '{"title":"Fine","start_s":10,"end_s":40,"confidence":0.8},'
            '{"title":"Past the end","start_s":120,"end_s":130,"confidence":0.8}]}}\n'
        ),
        encoding="utf-8",
    )
    result = import_gemini_analysis(project)
    assert result["skipped"] == 1
    with ClaimStore(project) as store:
        claims = store.claims(v1_artifact="gemini_events.jsonl")
    rejections = [claim for claim in claims if claim["payload"].get("rejection")]
    assert len(rejections) == 1
    assert rejections[0]["payload"]["rejection"]["item"]["item"]["title"] == "Past the end"


def test_heuristic_events_dual_write_byte_identical(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    append_jsonl(project / "tapes.jsonl", {"id": "video_000001", "filename": "t.mp4", "probe": {"duration_s": 120.0}})
    append_jsonl(
        project / "scenes.jsonl",
        {"source_video_id": "video_000001", "scene_type": "content", "start_s": 10.0, "end_s": 110.0},
    )
    result = build_heuristic_events(project)
    assert result["heuristic_events"] == 1
    diff = diff_v1_artifact(project, "heuristic_events.jsonl")
    assert diff["byte_identical"] is True
    with ClaimStore(project) as store:
        claims = store.claims(v1_artifact="heuristic_events.jsonl")
    assert claims[0]["producer"] == "local_heuristic"


def test_review_actions_dual_write_human_claims(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    append_jsonl(project / "face_clusters.jsonl", {"id": "fc_1", "review_status": "needs_review"})
    apply_review_actions(
        project,
        actions=[
            {
                "action": "label_face_cluster",
                "target_id": "fc_1",
                "payload": {"label": "Filip"},
                "reviewer": "phillip",
            }
        ],
    )
    with ClaimStore(project) as store:
        claims = store.claims(kind="human_action")
    assert len(claims) == 1
    assert claims[0]["producer"] == "phillip"
    assert claims[0]["payload"]["assertion"]["action"] == "label_face_cluster"
    diff = diff_v1_artifact(project, "corrections.jsonl")
    assert diff["byte_identical"] is True


def test_backfill_is_idempotent_and_covers_pre_substrate_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TAPESPLIT_CLAIMS", "0")
    project = _chunk_project(tmp_path)
    import_gemini_analysis(project)  # v1 rows exist, no claims
    monkeypatch.delenv("TAPESPLIT_CLAIMS")

    first = backfill_project_claims(project)
    assert first["backfilled"]["gemini_events.jsonl"] == 2
    second = backfill_project_claims(project)
    assert second["backfilled"]["gemini_events.jsonl"] == 0
    assert diff_v1_artifact(project, "gemini_events.jsonl")["byte_identical"] is True


def test_export_round_trip(tmp_path: Path):
    project = _chunk_project(tmp_path)
    import_gemini_analysis(project)
    with ClaimStore(project) as store:
        result = store.export_jsonl()
    rows = read_jsonl(Path(result["output"]))
    assert result["claims_exported"] == len(rows) == 2
    assert {row["id"] for row in rows} == {"claim_00000001", "claim_00000002"}
    assert all(row["payload"]["v1_row"]["id"].startswith("gem_event_") for row in rows)


# --- media index and source_media_id shim ---


def test_build_media_index_derives_tapes_and_keeps_other_media(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    append_jsonl(project / "tapes.jsonl", {"id": "video_000001", "filename": "t.mp4", "path": "/v/t.mp4", "probe": {"duration_s": 5.0}})
    append_jsonl(project / "media.jsonl", {"media_id": "photo_000001", "media_type": "photo", "path": "/p/a.jpg"})

    result = build_media_index(project)
    assert result["media_rows"] == 2 and result["tape_rows"] == 1
    rows = read_jsonl(project / "media.jsonl")
    assert rows[0] == {
        "media_id": "video_000001",
        "media_type": "tape",
        "filename": "t.mp4",
        "path": "/v/t.mp4",
        "relative_path": None,
        "probe": {"duration_s": 5.0},
    }
    assert rows[1]["media_type"] == "photo"


def test_source_media_id_shim_prefers_new_key():
    assert source_media_id({"source_media_id": "media_1", "source_video_id": "video_1"}) == "media_1"
    assert source_media_id({"source_video_id": "video_1"}) == "video_1"
    assert source_media_id({}) is None

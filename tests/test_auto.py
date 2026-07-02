import json
from pathlib import Path

import pytest

import tapesplit.auto as auto
from tapesplit.auto import (
    AutoOptions,
    PlannedStage,
    Stage,
    StageSkipped,
    load_pipeline_state,
    plan_auto,
    resolve_project_input,
    run_auto,
    save_pipeline_state,
)
from tapesplit.storage import append_jsonl, write_json


FULL_CAPS = {
    "ffmpeg": True,
    "ffprobe": True,
    "exiftool": True,
    "whisper_cli": True,
    "whisper_cpp_cli": True,
    "whisper_cpp_main": False,
    "whisper_cpp_model": "/models/ggml.bin",
    "speaker_diarization_pyannote": True,
    "speaker_diarization_speechbrain": True,
    "speaker_diarization_hf_token": True,
    "gemini_configured": True,
    "gemini_adc": True,
    "gemini_gcs_bucket": True,
    "visual_text_default_backend": "apple-vision",
    "visual_caption_default_backend": "transformers-blip",
    "visual_embedding_default_backend": "sentence-transformers",
    "face_detection_default_backend": "apple-vision",
    "face_embedding_default_backend": "arcface-insightface",
}


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "sample.tapesplit"
    project.mkdir()
    write_json(project / "manifest.json", {"schema_version": 1})
    append_jsonl(project / "tapes.jsonl", {"id": "video_000001", "probe": {"duration_s": 10.0}})
    return project


def _actions(plan: list[PlannedStage]) -> dict[str, str]:
    return {item.stage.name: item.action for item in plan}


def test_plan_all_stages_run_with_full_capabilities(tmp_path):
    project = _project(tmp_path)
    plan = plan_auto(project, AutoOptions(), FULL_CAPS, {"stages": {}})
    actions = _actions(plan)
    assert actions["ingest"] == "skip-done"  # tapes.jsonl already exists
    for name in ("scenes", "transcribe", "diarize", "gemini", "core-build", "finalize", "apply-suggestions"):
        assert actions[name] == "run"


def test_plan_marks_unavailable_and_blocks_dependents(tmp_path):
    project = _project(tmp_path)
    caps = dict(FULL_CAPS, ffmpeg=False)
    plan = plan_auto(project, AutoOptions(), caps, {"stages": {}})
    actions = _actions(plan)
    assert actions["non-content"] == "unavailable"
    assert actions["scenes"] == "unavailable"
    assert actions["visuals"] == "unavailable"
    assert actions["faces"] == "blocked"  # requires visuals
    assert actions["face-cluster"] == "blocked"
    assert actions["core-build"] == "run"  # derived stages still run
    assert actions["finalize"] == "run"


def test_plan_profiles_disable_kinds(tmp_path):
    project = _project(tmp_path)
    local = _actions(plan_auto(project, AutoOptions(profile="local"), FULL_CAPS, {"stages": {}}))
    assert local["gemini"] == "disabled"
    assert local["transcribe"] == "run"

    minimal = _actions(plan_auto(project, AutoOptions(profile="minimal"), FULL_CAPS, {"stages": {}}))
    assert minimal["gemini"] == "disabled"
    assert minimal["transcribe"] == "disabled"
    assert minimal["ocr"] == "disabled"
    assert minimal["scenes"] == "run"
    assert minimal["finalize"] == "run"


def test_plan_skip_only_and_suggestion_flags(tmp_path):
    project = _project(tmp_path)
    actions = _actions(
        plan_auto(project, AutoOptions(skip=("faces",)), FULL_CAPS, {"stages": {}})
    )
    assert actions["faces"] == "disabled"
    assert actions["face-cluster"] == "blocked"

    actions = _actions(
        plan_auto(project, AutoOptions(only=("scenes",)), FULL_CAPS, {"stages": {}})
    )
    assert actions["scenes"] == "run"
    assert actions["transcribe"] == "disabled"

    actions = _actions(
        plan_auto(project, AutoOptions(suggestions_tier=None), FULL_CAPS, {"stages": {}})
    )
    assert actions["apply-suggestions"] == "disabled"


def test_plan_unknown_stage_name_raises(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(ValueError, match="unknown stage name"):
        plan_auto(project, AutoOptions(skip=("nope",)), FULL_CAPS, {"stages": {}})


def test_plan_respects_state_and_force(tmp_path):
    project = _project(tmp_path)
    state = {"stages": {"scenes": {"status": "completed"}}}
    actions = _actions(plan_auto(project, AutoOptions(), FULL_CAPS, state))
    assert actions["scenes"] == "skip-done"

    actions = _actions(plan_auto(project, AutoOptions(force=True), FULL_CAPS, state))
    assert actions["scenes"] == "run"

    actions = _actions(plan_auto(project, AutoOptions(force_from="scenes"), FULL_CAPS, state))
    assert actions["scenes"] == "run"


def test_plan_adopts_existing_artifacts_without_state(tmp_path):
    project = _project(tmp_path)
    append_jsonl(project / "scenes.jsonl", {"source_video_id": "video_000001"})
    actions = _actions(plan_auto(project, AutoOptions(), FULL_CAPS, {"stages": {}}))
    assert actions["scenes"] == "skip-done"


def test_resolve_project_input(tmp_path):
    project = _project(tmp_path)
    resolved, source = resolve_project_input(project, None)
    assert resolved == project
    assert source is None

    video = tmp_path / "tape.mp4"
    video.write_bytes(b"fake")
    resolved, source = resolve_project_input(video, None)
    assert resolved == tmp_path / "tape.mp4.tapesplit"
    assert source == video

    not_project = tmp_path / "existing"
    not_project.mkdir()
    (not_project / "random.txt").write_text("hi")
    with pytest.raises(FileExistsError):
        resolve_project_input(video, not_project)


def _stub_stages(calls: list[str], *, fail: set[str] = frozenset(), skip: set[str] = frozenset()):
    def make_run(name):
        def run(context):
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} exploded")
            if name in skip:
                raise StageSkipped(f"{name} not needed")
            return {"ran": name}

        return run

    return [
        Stage(name="ingest", title="Ingest", kind="setup", produces=("tapes.jsonl",), run=make_run("ingest")),
        Stage(name="alpha", title="Alpha", kind="local", requires=("ingest",), run=make_run("alpha")),
        Stage(name="beta", title="Beta", kind="local", requires=("alpha",), run=make_run("beta")),
        Stage(
            name="core-build",
            title="Core",
            kind="derived",
            requires=("ingest",),
            always_run=True,
            run=make_run("core-build"),
        ),
        Stage(
            name="finalize",
            title="Finalize",
            kind="derived",
            requires=("ingest",),
            always_run=True,
            run=make_run("finalize"),
        ),
    ]


def test_run_auto_executes_and_persists_state(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(auto, "build_stages", lambda: _stub_stages(calls))
    monkeypatch.setattr(auto, "gather_capabilities", lambda: dict(FULL_CAPS))

    result = run_auto(project, AutoOptions(quiet=True))

    assert result["ok"] is True
    assert calls == ["alpha", "beta", "core-build", "finalize"]  # ingest adopted from artifacts
    state = load_pipeline_state(project)
    assert state["stages"]["alpha"]["status"] == "completed"
    assert state["stages"]["finalize"]["status"] == "completed"

    # Second run skips completed stages but re-runs always_run stages.
    calls.clear()
    result = run_auto(project, AutoOptions(quiet=True))
    assert calls == ["core-build", "finalize"]
    assert result["ok"] is True


def test_run_auto_failure_blocks_dependents_but_finishes(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(auto, "build_stages", lambda: _stub_stages(calls, fail={"alpha"}))
    monkeypatch.setattr(auto, "gather_capabilities", lambda: dict(FULL_CAPS))

    result = run_auto(project, AutoOptions(quiet=True))

    statuses = {row["name"]: row["status"] for row in result["stages"]}
    assert statuses["alpha"] == "failed"
    assert statuses["beta"] == "blocked"
    assert statuses["finalize"] == "completed"
    assert result["ok"] is True  # ingest/core-build/finalize all fine

    state = load_pipeline_state(project)
    assert state["stages"]["alpha"]["status"] == "failed"
    assert "exploded" in state["stages"]["alpha"]["error"]

    # A fixed stage resumes on the next run.
    calls.clear()
    monkeypatch.setattr(auto, "build_stages", lambda: _stub_stages(calls))
    result = run_auto(project, AutoOptions(quiet=True))
    statuses = {row["name"]: row["status"] for row in result["stages"]}
    assert statuses["alpha"] == "completed"
    assert statuses["beta"] == "completed"


def test_run_auto_stage_skipped_is_not_failure(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(auto, "build_stages", lambda: _stub_stages(calls, skip={"alpha"}))
    monkeypatch.setattr(auto, "gather_capabilities", lambda: dict(FULL_CAPS))

    result = run_auto(project, AutoOptions(quiet=True))
    statuses = {row["name"]: row["status"] for row in result["stages"]}
    assert statuses["alpha"] == "skipped"
    assert statuses["beta"] == "blocked"

    state = load_pipeline_state(project)
    assert state["stages"]["alpha"]["status"] == "skipped"
    assert state["stages"]["alpha"]["reason"] == "alpha not needed"


def test_run_auto_plan_only_writes_nothing(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(auto, "build_stages", lambda: _stub_stages(calls))
    monkeypatch.setattr(auto, "gather_capabilities", lambda: dict(FULL_CAPS))

    result = run_auto(project, AutoOptions(quiet=True, plan_only=True))
    assert calls == []
    assert not (project / "pipeline_state.json").exists()
    assert {item["stage"] for item in result["plan"]} == {"ingest", "alpha", "beta", "core-build", "finalize"}


def test_gemini_stage_budget_gate(tmp_path, monkeypatch):
    import tapesplit.gemini_adapter as gemini_adapter

    project = _project(tmp_path)
    monkeypatch.setattr(
        gemini_adapter,
        "estimate_project_videos",
        lambda *args, **kwargs: {
            "results": [{"source_video_id": "video_000001", "estimated_cost_usd": 42.0}]
        },
    )
    context = auto.StageContext(
        project=project,
        options=AutoOptions(max_cloud_usd=5.0),
        capabilities=dict(FULL_CAPS),
    )
    with pytest.raises(StageSkipped, match="exceeds"):
        auto._run_gemini_analyze(context)

    # Gate disabled: proceeds to analysis (stubbed).
    monkeypatch.setattr(gemini_adapter, "prepare_project_video_proxies", lambda project: {"proxies": 1})
    monkeypatch.setattr(
        gemini_adapter,
        "analyze_project_video",
        lambda project, source_video_id: {"source_video_id": source_video_id},
    )
    context.options.max_cloud_usd = None
    summary = auto._run_gemini_analyze(context)
    assert summary["videos_analyzed"] == 1


def test_gemini_stage_skips_when_all_analyzed(tmp_path):
    project = _project(tmp_path)
    append_jsonl(project / "gemini_analyses.jsonl", {"source_video_id": "video_000001"})
    context = auto.StageContext(project=project, options=AutoOptions(), capabilities=dict(FULL_CAPS))
    summary = auto._run_gemini_analyze(context)
    assert summary["videos_analyzed"] == 0
    assert "already analyzed" in summary["note"]


def test_save_and_load_pipeline_state_roundtrip(tmp_path):
    project = _project(tmp_path)
    state = {"stages": {"scenes": {"status": "completed"}}}
    save_pipeline_state(project, state)
    loaded = load_pipeline_state(project)
    assert loaded["stages"]["scenes"]["status"] == "completed"
    assert loaded["schema_version"] == 1

    (project / "pipeline_state.json").write_text("{broken", encoding="utf-8")
    assert load_pipeline_state(project) == {"schema_version": 1, "stages": {}}


def test_apply_suggestions_stage_iterates_until_no_new_acceptance(tmp_path, monkeypatch):
    import tapesplit.pipeline as pipeline
    import tapesplit.review_actions as review_actions

    project = _project(tmp_path)
    apply_counts = iter([2, 1, 0])
    applied_calls = []
    rebuild_calls = []

    def fake_apply(project_dir, **kwargs):
        count = next(apply_counts)
        applied_calls.append(kwargs["tier"])
        return {"actions_applied": count, "by_action": {"confirm_relationship": count} if count else {}}

    monkeypatch.setattr(review_actions, "apply_review_suggestions", fake_apply)
    monkeypatch.setattr(
        pipeline,
        "rebuild_project_outputs",
        lambda project_dir, **kwargs: rebuild_calls.append(True) or {"steps": []},
    )

    context = auto.StageContext(project=project, options=AutoOptions(), capabilities=dict(FULL_CAPS))
    summary = auto._run_apply_suggestions(context)

    assert summary["actions_applied"] == 3
    assert summary["by_action"] == {"confirm_relationship": 3}
    assert summary["passes"] == 3
    assert len(applied_calls) == 3
    assert len(rebuild_calls) == 2  # no rebuild after the empty final pass


def test_apply_suggestions_stage_disabled_raises_skip(tmp_path):
    project = _project(tmp_path)
    context = auto.StageContext(
        project=project,
        options=AutoOptions(suggestions_tier=None),
        capabilities=dict(FULL_CAPS),
    )
    with pytest.raises(StageSkipped):
        auto._run_apply_suggestions(context)


def test_interrupted_ingest_detection(tmp_path):
    project = tmp_path / "p.tapesplit"
    assert auto._is_interrupted_ingest(project) is False  # missing dir

    project.mkdir()
    write_json(project / "manifest.json", {"schema_version": 1})
    (project / "keyframes").mkdir()
    (project / "thumbnails").mkdir()
    assert auto._is_interrupted_ingest(project) is True  # scaffolding only

    append_jsonl(project / "scenes.jsonl", {"id": "x"})
    assert auto._is_interrupted_ingest(project) is False  # has derived artifacts

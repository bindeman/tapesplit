"""One-command pipeline orchestrator.

``tapesplit auto`` takes unorganized digitized tapes (a video file, a folder of
videos, or an existing ``.tapesplit`` project) and produces a complete
reviewable archive: canonical events, groups, relationships, faces, speakers,
search index, story, report, and visualization — with no further human input.

Design goals:

- **Capability aware.** Each stage declares what it needs (binaries, optional
  ML packages, cloud providers). Missing capabilities degrade the plan instead
  of failing it; a machine with nothing but ffmpeg still gets a browsable
  archive from deterministic stages plus heuristic events.
- **Resumable.** Per-stage status is tracked in ``pipeline_state.json`` inside
  the project. Re-running ``auto`` skips completed stages; per-video stages
  additionally skip already-processed videos, so an interrupted run continues
  where it stopped.
- **Failure isolating.** An optional stage that fails is recorded and skipped
  past; only stages that hard-require it are blocked. The archive is always
  built from whatever evidence exists.
- **Cost guarded.** Cloud video analysis is estimated first and skipped when
  the estimate exceeds ``--max-cloud-usd``.
- **Minimal-intervention.** After outputs are built, high-confidence review
  suggestions can be auto-accepted as durable corrections (``--suggestions``
  tier, default ``primary``), shrinking the human review queue to genuinely
  ambiguous items.
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tapesplit.env import load_dotenv
from tapesplit.storage import read_jsonl, write_json

PIPELINE_STATE_FILENAME = "pipeline_state.json"
PIPELINE_STATE_SCHEMA_VERSION = 1

PROFILES = ("auto", "local", "minimal")

# Stage kinds, from cheapest to most demanding.
KIND_SETUP = "setup"
KIND_LOCAL = "local"
KIND_LOCAL_ML = "local-ml"
KIND_CLOUD = "cloud"
KIND_DERIVED = "derived"

DEFAULT_MAX_CLOUD_USD = 10.0


class StageSkipped(Exception):
    """Raised inside a stage run to skip it intentionally (not a failure)."""


@dataclass
class AutoOptions:
    profile: str = "auto"
    out: Path | None = None
    window_seconds: float = 30.0
    language: str | None = None
    force: bool = False
    force_from: str | None = None
    skip: tuple[str, ...] = ()
    only: tuple[str, ...] = ()
    max_cloud_usd: float | None = DEFAULT_MAX_CLOUD_USD
    suggestions_tier: str | None = "primary"
    suggestions_min_confidence: float | None = None
    search_embedding_backend: str = "local-sparse"
    plan_only: bool = False
    as_json: bool = False
    quiet: bool = False


@dataclass(frozen=True)
class Stage:
    name: str
    title: str
    kind: str
    requires: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    always_run: bool = False
    availability: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    run: Callable[["StageContext"], dict[str, Any]] | None = None


@dataclass
class StageContext:
    project: Path
    options: AutoOptions
    capabilities: dict[str, Any]
    source_input: Path | None = None


@dataclass
class PlannedStage:
    stage: Stage
    action: str  # "run" | "skip-done" | "unavailable" | "disabled" | "blocked"
    reason: str = ""


@dataclass
class StageOutcome:
    name: str
    title: str
    kind: str
    status: str  # "completed" | "failed" | "skipped" | "unavailable" | "disabled" | "blocked"
    reason: str = ""
    error: str | None = None
    duration_s: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Capabilities


def gather_capabilities() -> dict[str, Any]:
    """Probe binaries, optional ML packages, and cloud provider config."""

    load_dotenv()
    from tapesplit.face_clustering import check_face_embedding_config
    from tapesplit.faces import check_face_detection_config
    from tapesplit.gemini_adapter import check_gemini_config
    from tapesplit.speakers import check_speaker_diarization_config
    from tapesplit.transcription import check_transcription_config
    from tapesplit.visual_captions import check_visual_caption_config
    from tapesplit.visual_embeddings import check_visual_embedding_config
    from tapesplit.visual_text import check_visual_text_config

    caps: dict[str, Any] = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "exiftool": shutil.which("exiftool") is not None,
    }
    caps.update(check_transcription_config())
    caps.update(check_speaker_diarization_config())
    caps.update(check_face_detection_config())
    caps.update(check_face_embedding_config())
    caps.update(check_visual_text_config())
    caps.update(check_visual_embedding_config())
    caps.update(check_visual_caption_config())
    try:
        caps.update(check_gemini_config())
    except Exception as exc:  # config parsing should never block local stages
        caps.update({"gemini_configured": False, "gemini_adc": False, "gemini_error": str(exc)})
    return caps


def _has_transcription_engine(caps: dict[str, Any]) -> bool:
    whisper_cpp = bool(caps.get("whisper_cpp_model")) and (
        caps.get("whisper_cpp_cli") or caps.get("whisper_cpp_main")
    )
    return bool(whisper_cpp or caps.get("whisper_cli"))


def _available_ffmpeg(caps: dict[str, Any]) -> tuple[bool, str]:
    if caps.get("ffmpeg") and caps.get("ffprobe"):
        return True, ""
    return False, "ffmpeg/ffprobe not on PATH (brew install ffmpeg)"


def _available_exiftool(caps: dict[str, Any]) -> tuple[bool, str]:
    if caps.get("exiftool"):
        return True, ""
    return False, "exiftool not on PATH (brew install exiftool)"


def _available_transcription(caps: dict[str, Any]) -> tuple[bool, str]:
    if not caps.get("ffmpeg"):
        return False, "ffmpeg required for audio extraction"
    if _has_transcription_engine(caps):
        return True, ""
    return False, "no Whisper engine (install whisper.cpp + WHISPER_CPP_MODEL, or openai-whisper)"


def _available_diarization(caps: dict[str, Any]) -> tuple[bool, str]:
    if not caps.get("ffmpeg"):
        return False, "ffmpeg required for audio extraction"
    if caps.get("speaker_diarization_pyannote") and caps.get("speaker_diarization_hf_token"):
        return True, ""
    if caps.get("speaker_diarization_speechbrain"):
        return True, ""
    return False, "no diarization backend (install .[speaker-ai]; pyannote also needs HF_TOKEN)"


def _available_gemini(caps: dict[str, Any]) -> tuple[bool, str]:
    if not caps.get("gemini_configured"):
        return False, "Vertex Gemini not configured (GEMINI_USE_VERTEX/GOOGLE_CLOUD_PROJECT/GEMINI_MODEL)"
    if not caps.get("gemini_adc"):
        return False, "no Google ADC credentials (gcloud auth application-default login)"
    if not caps.get("gemini_gcs_bucket"):
        return False, "GEMINI_GCS_BUCKET not configured"
    if not caps.get("ffmpeg"):
        return False, "ffmpeg required for upload proxies"
    return True, ""


def _available_backend(key: str, hint: str) -> Callable[[dict[str, Any]], tuple[bool, str]]:
    def check(caps: dict[str, Any]) -> tuple[bool, str]:
        backend = str(caps.get(key) or "unavailable")
        if backend != "unavailable":
            return True, ""
        return False, hint

    return check


# ---------------------------------------------------------------------------
# Stage run functions


def _run_ingest(context: StageContext) -> dict[str, Any]:
    from tapesplit.ingest import ingest

    tapes_path = context.project / "tapes.jsonl"
    if tapes_path.exists():
        tapes = read_jsonl(tapes_path)
        expected = _manifest_video_count(context.project)
        if expected is not None and len(tapes) < expected:
            # Interrupted mid-ingest: some videos never made it into the
            # project. Redo ingest when that is safe, otherwise fail loudly
            # instead of silently dropping tapes.
            if context.source_input is not None and _is_interrupted_ingest(context.project):
                return ingest(
                    input_path=context.source_input,
                    out_path=context.project,
                    window_seconds=context.options.window_seconds,
                    force=True,
                )
            raise RuntimeError(
                f"project has {len(tapes)} of {expected} ingested videos "
                f"(interrupted ingest); remove {context.project} to re-ingest"
            )
        return {"videos": len(tapes), "note": "project already ingested"}
    if context.source_input is None:
        raise RuntimeError("project has no tapes.jsonl and no source input was provided")
    return ingest(
        input_path=context.source_input,
        out_path=context.project,
        window_seconds=context.options.window_seconds,
        force=_is_interrupted_ingest(context.project),
    )


def _manifest_video_count(project: Path) -> int | None:
    manifest_path = project / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        count = manifest.get("video_count")
        return int(count) if count is not None else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _is_interrupted_ingest(project: Path) -> bool:
    """True when the project dir holds only ingest-era files (no artifacts).

    An interrupt during ingest leaves a half-created project that plain
    ``ingest`` refuses to overwrite. Redoing it (rmtree + re-ingest) is safe
    only while nothing derived exists yet.
    """

    if not project.exists():
        return False
    scaffolding = {
        "manifest.json",
        "tapes.jsonl",
        "windows.jsonl",
        "keyframes",
        "thumbnails",
        "pipeline_state.json",
        ".DS_Store",
    }
    for entry in project.iterdir():
        if entry.name not in scaffolding:
            return False
    return True


def _run_exif(context: StageContext) -> dict[str, Any]:
    from tapesplit.media_metadata import extract_exif_for_project

    return extract_exif_for_project(context.project)


def _run_non_content(context: StageContext) -> dict[str, Any]:
    from tapesplit.non_content import detect_non_content_for_project

    return detect_non_content_for_project(context.project)


def _run_scenes(context: StageContext) -> dict[str, Any]:
    from tapesplit.scenes import detect_scenes_for_project

    return detect_scenes_for_project(context.project)


def _run_transcribe(context: StageContext) -> dict[str, Any]:
    from tapesplit.transcription import transcribe_project_local

    return transcribe_project_local(
        context.project,
        engine="auto",
        language=context.options.language,
        skip_existing=True,
    )


def _run_diarize(context: StageContext) -> dict[str, Any]:
    from tapesplit.speakers import diarize_project_speakers

    project = context.project
    done = {
        str(row.get("source_video_id"))
        for row in read_jsonl(project / "speaker_segments.jsonl")
        if row.get("source_video_id")
    }
    tapes = read_jsonl(project / "tapes.jsonl")
    pending = [str(tape.get("id")) for tape in tapes if tape.get("id") and str(tape.get("id")) not in done]
    if not pending:
        return {"videos_diarized": 0, "note": "all videos already diarized"}

    results = []
    errors = []
    for source_video_id in pending:
        try:
            results.append(diarize_project_speakers(project, source_video_id=source_video_id))
        except Exception as exc:
            errors.append({"source_video_id": source_video_id, "error": str(exc)})
    if errors and not results:
        raise RuntimeError(f"diarization failed for all {len(errors)} videos: {errors[0]['error']}")
    summary: dict[str, Any] = {"videos_diarized": len(results)}
    backends = {str(result.get("backend")) for result in results if result.get("backend")}
    if backends:
        summary["backends"] = sorted(backends)
    if errors:
        summary["videos_failed"] = errors
    return summary


def _run_gemini_analyze(context: StageContext) -> dict[str, Any]:
    from tapesplit.gemini_adapter import (
        analyze_project_video,
        estimate_project_videos,
        prepare_project_video_proxies,
    )

    project = context.project
    options = context.options
    analyzed = {
        str(row.get("source_video_id"))
        for row in read_jsonl(project / "gemini_analyses.jsonl")
        if row.get("source_video_id")
    }
    tapes = read_jsonl(project / "tapes.jsonl")
    pending = [str(tape.get("id")) for tape in tapes if tape.get("id") and str(tape.get("id")) not in analyzed]
    if not pending:
        return {"videos_analyzed": 0, "note": "all videos already analyzed"}

    estimated_cost: float | None
    try:
        estimate = estimate_project_videos(project, all_videos=True)
        pending_results = [
            result
            for result in estimate.get("results", [])
            if str(result.get("source_video_id")) in set(pending)
        ]
        costs = [result.get("estimated_cost_usd") for result in pending_results]
        estimated_cost = None if any(cost is None for cost in costs) else round(sum(costs), 4)
    except Exception as exc:
        estimated_cost = None
        estimate_error = str(exc)
    else:
        estimate_error = None

    if options.max_cloud_usd is not None:
        if estimated_cost is None:
            reason = estimate_error or "cost rates unknown"
            raise StageSkipped(
                f"could not estimate cloud cost ({reason}); "
                "pass --max-cloud-usd -1 to run without the estimate gate"
            )
        if estimated_cost > options.max_cloud_usd:
            raise StageSkipped(
                f"estimated ${estimated_cost:.2f} for {len(pending)} videos exceeds "
                f"--max-cloud-usd {options.max_cloud_usd:.2f}"
            )

    prepare_project_video_proxies(project)
    results = []
    errors = []
    for source_video_id in pending:
        try:
            results.append(analyze_project_video(project, source_video_id=source_video_id))
        except Exception as exc:
            errors.append({"source_video_id": source_video_id, "error": str(exc)})
    if errors and not results:
        raise RuntimeError(f"gemini analysis failed for all {len(errors)} videos: {errors[0]['error']}")
    summary = {
        "videos_analyzed": len(results),
        "estimated_cost_usd": estimated_cost,
    }
    if errors:
        summary["videos_failed"] = errors
    return summary


def _run_core_build(context: StageContext) -> dict[str, Any]:
    from tapesplit.pipeline import rebuild_project_outputs

    has_gemini = bool(read_jsonl(context.project / "gemini_analyses.jsonl"))
    result = rebuild_project_outputs(
        context.project,
        import_gemini=has_gemini,
        import_all_gemini=has_gemini,
        core_only=True,
    )
    return _compact_rebuild_summary(result, {"imported_gemini": has_gemini})


def _run_visuals(context: StageContext) -> dict[str, Any]:
    from tapesplit.visual_assets import extract_visual_assets_for_project

    return extract_visual_assets_for_project(context.project)


def _run_ocr(context: StageContext) -> dict[str, Any]:
    from tapesplit.visual_text import detect_text_for_project

    return detect_text_for_project(context.project, backend="auto")


def _run_captions(context: StageContext) -> dict[str, Any]:
    from tapesplit.visual_captions import caption_visual_assets_for_project

    return caption_visual_assets_for_project(context.project, backend="auto")


def _run_visual_embed(context: StageContext) -> dict[str, Any]:
    from tapesplit.visual_embeddings import embed_visual_assets_for_project

    return embed_visual_assets_for_project(context.project, backend="auto")


def _run_visual_similarity(context: StageContext) -> dict[str, Any]:
    from tapesplit.visual_embeddings import build_visual_similarity_for_project

    return build_visual_similarity_for_project(context.project)


def _run_faces(context: StageContext) -> dict[str, Any]:
    from tapesplit.faces import detect_face_thumbnails_for_project

    return detect_face_thumbnails_for_project(context.project, backend="auto")


def _run_face_cluster(context: StageContext) -> dict[str, Any]:
    from tapesplit.face_clustering import cluster_faces_for_project

    if not read_jsonl(context.project / "face_observations.jsonl"):
        raise StageSkipped("no face observations detected")
    return cluster_faces_for_project(context.project, embedding_backend="auto")


def _run_finalize(context: StageContext) -> dict[str, Any]:
    from tapesplit.pipeline import rebuild_project_outputs

    result = rebuild_project_outputs(
        context.project,
        import_gemini=False,
        embedding_backend=context.options.search_embedding_backend,
    )
    return _compact_rebuild_summary(result, {})


MAX_SUGGESTION_PASSES = 3


def _run_apply_suggestions(context: StageContext) -> dict[str, Any]:
    from tapesplit.pipeline import rebuild_project_outputs
    from tapesplit.review_actions import apply_review_suggestions

    tier = context.options.suggestions_tier
    if not tier:
        raise StageSkipped("suggestion auto-acceptance disabled")

    # Accepted actions can unlock further ones: e.g. a merge_person identity
    # bridge lets a previously gated relationship pass the safe policy after
    # the rebuild regenerates candidates with resolved identities. Iterate
    # until a pass accepts nothing (bounded, since targets close as they are
    # accepted).
    passes = []
    total_applied = 0
    by_action: dict[str, int] = {}
    last_skipped: Any = None
    for _ in range(MAX_SUGGESTION_PASSES):
        applied = apply_review_suggestions(
            context.project,
            tier=tier,
            min_confidence=context.options.suggestions_min_confidence,
            reviewer="auto-pipeline",
            policy="safe",
        )
        count = applied.get("actions_applied", 0)
        passes.append({"actions_applied": count, "by_action": applied.get("by_action", {})})
        last_skipped = applied.get("skipped")
        if not count:
            break
        total_applied += count
        for action, action_count in (applied.get("by_action") or {}).items():
            by_action[action] = by_action.get(action, 0) + action_count
        # Corrections mutated core artifacts; rebuild derived outputs so the
        # exported archive and review queue reflect the accepted guesses
        # before the next pass re-reads visualization.json.
        rebuild_project_outputs(
            context.project,
            import_gemini=False,
            embedding_backend=context.options.search_embedding_backend,
        )

    return {
        "tier": tier,
        "actions_applied": total_applied,
        "by_action": by_action,
        "passes": len(passes),
        "skipped": last_skipped,
    }


def _compact_rebuild_summary(result: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    steps = [step.get("step") for step in result.get("steps", [])]
    summary = {"steps": steps}
    for step in result.get("steps", []):
        name = step.get("step")
        payload = step.get("result") or {}
        if name == "stitch_events":
            summary["canonical_events"] = payload.get("canonical_events")
        elif name == "build_heuristic_events":
            summary["heuristic_events"] = payload.get("heuristic_events")
        elif name == "gemini_import":
            summary["gemini_events"] = payload.get("events")
    summary.update(extra)
    return summary


# ---------------------------------------------------------------------------
# Stage registry


def build_stages() -> list[Stage]:
    return [
        Stage(
            name="ingest",
            title="Ingest videos",
            kind=KIND_SETUP,
            produces=("tapes.jsonl",),
            availability=lambda caps: (
                (True, "") if caps.get("ffprobe") else (False, "ffprobe not on PATH (brew install ffmpeg)")
            ),
            run=_run_ingest,
        ),
        Stage(
            name="exif",
            title="Extract EXIF metadata",
            kind=KIND_LOCAL,
            requires=("ingest",),
            produces=("media_metadata.jsonl",),
            availability=_available_exiftool,
            run=_run_exif,
        ),
        Stage(
            name="non-content",
            title="Detect non-content (blue/static)",
            kind=KIND_LOCAL,
            requires=("ingest",),
            produces=("non_content_ranges.jsonl",),
            availability=_available_ffmpeg,
            run=_run_non_content,
        ),
        Stage(
            name="scenes",
            title="Detect scenes",
            kind=KIND_LOCAL,
            requires=("ingest",),
            after=("non-content",),
            produces=("scenes.jsonl",),
            availability=_available_ffmpeg,
            run=_run_scenes,
        ),
        Stage(
            name="transcribe",
            title="Transcribe audio",
            kind=KIND_LOCAL_ML,
            requires=("ingest",),
            produces=("transcript_segments.jsonl",),
            availability=_available_transcription,
            run=_run_transcribe,
        ),
        Stage(
            name="diarize",
            title="Diarize speakers",
            kind=KIND_LOCAL_ML,
            requires=("ingest",),
            after=("transcribe",),
            produces=("speaker_segments.jsonl",),
            availability=_available_diarization,
            run=_run_diarize,
        ),
        Stage(
            name="gemini",
            title="Analyze videos (Vertex Gemini)",
            kind=KIND_CLOUD,
            requires=("ingest",),
            after=("non-content",),
            produces=("gemini_analyses.jsonl",),
            availability=_available_gemini,
            run=_run_gemini_analyze,
        ),
        Stage(
            name="core-build",
            title="Import analyses + stitch events",
            kind=KIND_DERIVED,
            requires=("ingest",),
            after=("gemini", "transcribe", "diarize", "scenes", "non-content", "exif"),
            always_run=True,
            run=_run_core_build,
        ),
        Stage(
            name="visuals",
            title="Extract keyframes + thumbnails",
            kind=KIND_LOCAL,
            requires=("ingest",),
            after=("core-build",),
            produces=("visual_assets.jsonl",),
            availability=_available_ffmpeg,
            run=_run_visuals,
        ),
        Stage(
            name="ocr",
            title="Detect on-screen text",
            kind=KIND_LOCAL_ML,
            requires=("visuals",),
            produces=("visual_text_observations.jsonl",),
            availability=_available_backend(
                "visual_text_default_backend",
                "no OCR backend (Apple Vision requires macOS + .[macos])",
            ),
            run=_run_ocr,
        ),
        Stage(
            name="captions",
            title="Caption keyframes",
            kind=KIND_LOCAL_ML,
            requires=("visuals",),
            produces=("visual_captions.jsonl",),
            availability=_available_backend(
                "visual_caption_default_backend",
                "no caption backend (install .[visual-ai] or Ollama)",
            ),
            run=_run_captions,
        ),
        Stage(
            name="visual-embed",
            title="Embed keyframes",
            kind=KIND_LOCAL_ML,
            requires=("visuals",),
            produces=("visual_embeddings.jsonl",),
            availability=_available_backend(
                "visual_embedding_default_backend",
                "no visual embedding backend (install .[visual-ai])",
            ),
            run=_run_visual_embed,
        ),
        Stage(
            name="visual-similarity",
            title="Build visual similarity edges",
            kind=KIND_LOCAL,
            requires=("visual-embed",),
            produces=("visual_similarity_edges.jsonl",),
            run=_run_visual_similarity,
        ),
        Stage(
            name="faces",
            title="Detect faces",
            kind=KIND_LOCAL_ML,
            requires=("visuals",),
            produces=("face_observations.jsonl",),
            availability=_available_backend(
                "face_detection_default_backend",
                "no face detection backend (install .[vision] or .[macos])",
            ),
            run=_run_faces,
        ),
        Stage(
            name="face-cluster",
            title="Cluster faces",
            kind=KIND_LOCAL_ML,
            requires=("faces",),
            produces=("face_clusters.jsonl",),
            availability=_available_backend(
                "face_embedding_default_backend",
                "no face embedding backend (install .[face-ai] or .[vision])",
            ),
            run=_run_face_cluster,
        ),
        Stage(
            name="finalize",
            title="Build archive (groups, search, story, report, viz)",
            kind=KIND_DERIVED,
            requires=("ingest",),
            after=(
                "core-build",
                "ocr",
                "captions",
                "visual-similarity",
                "face-cluster",
                "diarize",
            ),
            always_run=True,
            run=_run_finalize,
        ),
        Stage(
            name="apply-suggestions",
            title="Auto-accept high-confidence suggestions",
            kind=KIND_DERIVED,
            requires=("finalize",),
            always_run=True,
            run=_run_apply_suggestions,
        ),
    ]


def stage_names() -> list[str]:
    return [stage.name for stage in build_stages()]


# ---------------------------------------------------------------------------
# State


def load_pipeline_state(project: Path) -> dict[str, Any]:
    path = project / PIPELINE_STATE_FILENAME
    if not path.exists():
        return {"schema_version": PIPELINE_STATE_SCHEMA_VERSION, "stages": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PIPELINE_STATE_SCHEMA_VERSION, "stages": {}}
    if not isinstance(state, dict) or not isinstance(state.get("stages"), dict):
        return {"schema_version": PIPELINE_STATE_SCHEMA_VERSION, "stages": {}}
    return state


def save_pipeline_state(project: Path, state: dict[str, Any]) -> None:
    state["schema_version"] = PIPELINE_STATE_SCHEMA_VERSION
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(project / PIPELINE_STATE_FILENAME, state)


# ---------------------------------------------------------------------------
# Planning


def resolve_project_input(input_path: Path, out: Path | None) -> tuple[Path, Path | None]:
    """Return (project_dir, source_input). source_input is None when resuming."""

    source = input_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"input path does not exist: {source}")
    if source.is_dir() and (source / "manifest.json").exists():
        return source, None
    from tapesplit.ingest import default_project_path

    project = (out or default_project_path(source)).expanduser().resolve()
    if project.exists():
        if (project / "tapes.jsonl").exists() or (project / "manifest.json").exists():
            return project, source
        raise FileExistsError(
            f"output directory exists but is not a tapesplit project: {project}; "
            "remove it or pass --out"
        )
    return project, source


def plan_auto(
    project: Path,
    options: AutoOptions,
    capabilities: dict[str, Any],
    state: dict[str, Any],
) -> list[PlannedStage]:
    stages = build_stages()
    known = {stage.name for stage in stages}
    for name in (*options.skip, *options.only):
        if name not in known:
            raise ValueError(f"unknown stage name: {name} (known: {', '.join(sorted(known))})")
    if options.force_from is not None and options.force_from not in known:
        raise ValueError(f"unknown stage name: {options.force_from}")

    disabled_kinds = set()
    if options.profile == "local":
        disabled_kinds = {KIND_CLOUD}
    elif options.profile == "minimal":
        disabled_kinds = {KIND_CLOUD, KIND_LOCAL_ML}

    force_index = None
    if options.force_from is not None:
        force_index = next(i for i, stage in enumerate(stages) if stage.name == options.force_from)

    planned: list[PlannedStage] = []
    unrunnable: set[str] = set()
    stage_states = state.get("stages", {})
    for index, stage in enumerate(stages):
        forced = options.force or (force_index is not None and index >= force_index)

        if options.only and stage.name not in options.only:
            planned.append(PlannedStage(stage, "disabled", "not in --only"))
            # A stage excluded by --only is not "unrunnable" for dependents:
            # its prior outputs may exist. Dependents check completion at runtime.
            continue
        if stage.name in options.skip:
            planned.append(PlannedStage(stage, "disabled", "skipped by --skip"))
            unrunnable.add(stage.name)
            continue
        if stage.kind in disabled_kinds:
            planned.append(PlannedStage(stage, "disabled", f"excluded by --profile {options.profile}"))
            unrunnable.add(stage.name)
            continue
        if stage.name == "apply-suggestions" and not options.suggestions_tier:
            planned.append(PlannedStage(stage, "disabled", "disabled by --no-suggestions"))
            continue

        if stage.availability is not None:
            ok, reason = stage.availability(capabilities)
            if not ok:
                planned.append(PlannedStage(stage, "unavailable", reason))
                unrunnable.add(stage.name)
                continue

        blocked_by = [name for name in stage.requires if name in unrunnable]
        if blocked_by:
            done = _stage_done(stage, stage_states, project)
            if done and not forced:
                planned.append(PlannedStage(stage, "skip-done", "already completed"))
                continue
            planned.append(PlannedStage(stage, "blocked", f"requires {', '.join(blocked_by)}"))
            unrunnable.add(stage.name)
            continue

        if not forced and not stage.always_run and _stage_done(stage, stage_states, project):
            planned.append(PlannedStage(stage, "skip-done", "already completed"))
            continue

        planned.append(PlannedStage(stage, "run"))
    return planned


def _stage_done(stage: Stage, stage_states: dict[str, Any], project: Path) -> bool:
    record = stage_states.get(stage.name)
    if isinstance(record, dict) and record.get("status") == "completed":
        return True
    # No state (e.g. project built with manual commands): adopt existing artifacts.
    if record is None and stage.produces:
        return all((project / artifact).exists() for artifact in stage.produces)
    return False


# ---------------------------------------------------------------------------
# Execution


def run_auto(
    input_path: Path,
    options: AutoOptions,
    *,
    emit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def say(message: str) -> None:
        if not options.quiet:
            (emit or print)(message)

    started = time.monotonic()
    project, source_input = resolve_project_input(input_path, options.out)
    capabilities = gather_capabilities()

    project_exists = project.exists()
    state = load_pipeline_state(project) if project_exists else {"schema_version": PIPELINE_STATE_SCHEMA_VERSION, "stages": {}}
    plan = plan_auto(project, options, capabilities, state)

    runnable = [item for item in plan if item.action == "run"]
    say(f"tapesplit auto · {project}")
    say(
        f"profile {options.profile} · {len(runnable)}/{len(plan)} stages to run"
        + (f" · resuming from existing project" if source_input is None else "")
    )
    if options.plan_only:
        for item in plan:
            say(_format_plan_line(item))
        return {
            "project": str(project),
            "profile": options.profile,
            "plan": [
                {"stage": item.stage.name, "title": item.stage.title, "action": item.action, "reason": item.reason}
                for item in plan
            ],
        }

    context = StageContext(
        project=project,
        options=options,
        capabilities=capabilities,
        source_input=source_input,
    )

    outcomes: list[StageOutcome] = []
    completed: set[str] = set()
    intentionally_skipped: set[str] = set()
    failed: set[str] = set()
    stages_by_name = {item.stage.name: item.stage for item in plan}

    def requirement_met(name: str) -> bool:
        if name in completed:
            return True
        # A dependency outside this run's plan (e.g. excluded by --only) may
        # already be satisfied by recorded state or existing artifacts.
        requirement = stages_by_name.get(name)
        return requirement is not None and _stage_done(requirement, state.get("stages", {}), project)

    def persist_state() -> None:
        if project.exists():
            save_pipeline_state(project, state)

    total = len(plan)
    for index, item in enumerate(plan, start=1):
        stage = item.stage
        prefix = f"[{index:>2}/{total}] {stage.title:<44}"
        if item.action == "skip-done":
            completed.add(stage.name)
            outcomes.append(StageOutcome(stage.name, stage.title, stage.kind, "skipped", item.reason))
            say(f"{prefix} = already done")
            continue
        if item.action in ("unavailable", "disabled", "blocked"):
            outcomes.append(StageOutcome(stage.name, stage.title, stage.kind, item.action, item.reason))
            say(f"{prefix} - {item.action}: {item.reason}")
            continue

        missing = [name for name in stage.requires if not requirement_met(name)]
        if missing:
            reason = f"requires {', '.join(missing)}"
            outcomes.append(StageOutcome(stage.name, stage.title, stage.kind, "blocked", reason))
            say(f"{prefix} - blocked: {reason}")
            continue

        stage_started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            summary = stage.run(context) if stage.run else {}
        except StageSkipped as exc:
            outcome = StageOutcome(stage.name, stage.title, stage.kind, "skipped", str(exc))
            outcomes.append(outcome)
            intentionally_skipped.add(stage.name)
            _record_stage(state, stage.name, "skipped", started_at, reason=str(exc))
            persist_state()
            say(f"{prefix} - skipped: {exc}")
            continue
        except KeyboardInterrupt:
            _record_stage(state, stage.name, "interrupted", started_at)
            persist_state()
            say(f"{prefix} ! interrupted — rerun `tapesplit auto` to resume")
            raise
        except Exception as exc:
            duration = time.monotonic() - stage_started
            error = f"{type(exc).__name__}: {exc}"
            outcome = StageOutcome(
                stage.name, stage.title, stage.kind, "failed", error=error, duration_s=round(duration, 1)
            )
            outcomes.append(outcome)
            failed.add(stage.name)
            _record_stage(
                state,
                stage.name,
                "failed",
                started_at,
                error=error,
                traceback_tail="".join(traceback.format_exception(exc)[-3:]).strip()[:2000],
                duration_s=round(duration, 1),
            )
            persist_state()
            say(f"{prefix} x failed ({duration:.0f}s): {error}")
            continue

        duration = time.monotonic() - stage_started
        completed.add(stage.name)
        outcome = StageOutcome(
            stage.name,
            stage.title,
            stage.kind,
            "completed",
            duration_s=round(duration, 1),
            summary=summary if isinstance(summary, dict) else {},
        )
        outcomes.append(outcome)
        _record_stage(
            state,
            stage.name,
            "completed",
            started_at,
            duration_s=round(duration, 1),
            summary=_truncate_summary(summary),
        )
        persist_state()
        say(f"{prefix} + done ({_format_duration(duration)}){_format_highlights(stage.name, summary)}")

    elapsed = time.monotonic() - started
    metrics = collect_project_metrics(project)
    if options.only:
        # A targeted run succeeds when every requested stage ran (or was
        # already done / intentionally skipped), not when the whole archive
        # was rebuilt.
        ok = all(name in completed or name in intentionally_skipped for name in options.only)
    else:
        ok = all(name in completed for name in ("ingest", "core-build", "finalize"))
    result = {
        "project": str(project),
        "profile": options.profile,
        "ok": ok,
        "elapsed_s": round(elapsed, 1),
        "stages": [outcome.__dict__ for outcome in outcomes],
        "metrics": metrics,
    }

    say("")
    say(f"{'archive ready' if ok else 'finished with gaps'} in {_format_duration(elapsed)} · {project}")
    say(f"  {_format_metrics_line(metrics)}")
    if not ok:
        problems = [outcome for outcome in outcomes if outcome.status in ("failed", "blocked")]
        for outcome in problems[:6]:
            say(f"  ! {outcome.name}: {outcome.error or outcome.reason}")
    say(f"  next: tapesplit ui {project}")
    return result


def _record_stage(
    state: dict[str, Any],
    name: str,
    status: str,
    started_at: str,
    **extra: Any,
) -> None:
    stages = state.setdefault("stages", {})
    record = {
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    record.update({key: value for key, value in extra.items() if value is not None})
    stages[name] = record


def _truncate_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    try:
        text = json.dumps(summary)
    except (TypeError, ValueError):
        return {}
    if len(text) <= 4000:
        return summary
    return {"truncated": True, "keys": sorted(summary.keys())}


def _format_plan_line(item: PlannedStage) -> str:
    marker = {"run": "+", "skip-done": "=", "unavailable": "-", "disabled": "-", "blocked": "!"}[item.action]
    reason = f" ({item.reason})" if item.reason else ""
    return f"  {marker} {item.stage.name:<18} {item.action}{reason}"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


_HIGHLIGHT_KEYS = (
    ("videos", "videos"),
    ("videos_analyzed", "analyzed"),
    ("videos_diarized", "diarized"),
    ("videos_transcribed", "transcribed"),
    ("segments_written", "segments"),
    ("canonical_events", "events"),
    ("heuristic_events", "heuristic events"),
    ("scenes", "scenes"),
    ("segments", "segments"),
    ("ranges", "ranges"),
    ("assets", "assets"),
    ("observations", "observations"),
    ("captions", "captions"),
    ("embeddings", "embeddings"),
    ("edges", "edges"),
    ("faces", "faces"),
    ("clusters", "clusters"),
    ("actions_applied", "accepted"),
    ("estimated_cost_usd", "$"),
)


def _format_highlights(stage_name: str, summary: Any) -> str:
    if not isinstance(summary, dict):
        return ""
    parts = []
    for key, label in _HIGHLIGHT_KEYS:
        value = summary.get(key)
        if value is None:
            continue
        if key == "estimated_cost_usd":
            parts.append(f"~${value}")
        else:
            parts.append(f"{value} {label}")
        if len(parts) >= 3:
            break
    return f" · {', '.join(parts)}" if parts else ""


# ---------------------------------------------------------------------------
# Metrics / status


def collect_project_metrics(project: Path) -> dict[str, Any]:
    counts = {
        "videos": len(read_jsonl(project / "tapes.jsonl")),
        "scenes": len(read_jsonl(project / "scenes.jsonl")),
        "transcript_segments": len(read_jsonl(project / "transcript_segments.jsonl")),
        "speaker_segments": len(read_jsonl(project / "speaker_segments.jsonl")),
        "canonical_events": len(read_jsonl(project / "canonical_events.jsonl")),
        "heuristic_events": len(read_jsonl(project / "heuristic_events.jsonl")),
        "albums": len(read_jsonl(project / "albums.jsonl")),
        "people_groups": len(read_jsonl(project / "people_groups.jsonl")),
        "place_groups": len(read_jsonl(project / "place_groups.jsonl")),
        "face_observations": len(read_jsonl(project / "face_observations.jsonl")),
        "face_clusters": len(read_jsonl(project / "face_clusters.jsonl")),
        "relationship_candidates": len(read_jsonl(project / "relationship_candidates.jsonl")),
        "corrections": len(read_jsonl(project / "corrections.jsonl")),
    }
    duration = 0.0
    for tape in read_jsonl(project / "tapes.jsonl"):
        probe = tape.get("probe") if isinstance(tape.get("probe"), dict) else {}
        try:
            duration += float(probe.get("duration_s") or 0.0)
        except (TypeError, ValueError):
            pass
    counts["total_duration_s"] = round(duration, 1)

    viz_path = project / "visualization.json"
    if viz_path.exists():
        try:
            viz = json.loads(viz_path.read_text(encoding="utf-8"))
            timeline = viz.get("timeline")
            if isinstance(timeline, dict):
                counts["timeline_events"] = len(timeline.get("events") or [])
            else:
                counts["timeline_events"] = len(timeline or [])
            counts["people"] = len(viz.get("people") or [])
            counts["places"] = len(viz.get("places") or [])
            counts["review_primary"] = len(viz.get("review_queue") or [])
            counts["review_backlog"] = len(viz.get("review_backlog") or [])
        except (OSError, json.JSONDecodeError):
            pass

    try:
        from tapesplit.costs import summarize_project_costs

        costs = summarize_project_costs(project)
        counts["estimated_cost_usd"] = costs.get("estimated_total_cost_usd")
    except Exception:
        pass
    return counts


def _format_metrics_line(metrics: dict[str, Any]) -> str:
    parts = []
    for key, label in (
        ("videos", "videos"),
        ("canonical_events", "events"),
        ("timeline_events", "timeline"),
        ("people", "people"),
        ("places", "places"),
        ("face_clusters", "face clusters"),
        ("review_primary", "review items"),
    ):
        value = metrics.get(key)
        if value:
            parts.append(f"{value} {label}")
    cost = metrics.get("estimated_cost_usd")
    if cost:
        parts.append(f"~${cost:.2f} spent")
    return " · ".join(parts) if parts else "empty project"


def project_status(project_dir: Path) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if not (project / "manifest.json").exists() and not (project / "tapes.jsonl").exists():
        raise FileNotFoundError(f"not a tapesplit project: {project}")
    state = load_pipeline_state(project)
    stage_rows = []
    stage_states = state.get("stages", {})
    for stage in build_stages():
        record = stage_states.get(stage.name) or {}
        done_without_state = not record and _stage_done(stage, stage_states, project)
        stage_rows.append(
            {
                "stage": stage.name,
                "title": stage.title,
                "kind": stage.kind,
                "status": record.get("status") or ("completed" if done_without_state else "pending"),
                "finished_at": record.get("finished_at"),
                "duration_s": record.get("duration_s"),
                "reason": record.get("reason") or record.get("error"),
            }
        )
    return {
        "project": str(project),
        "stages": stage_rows,
        "metrics": collect_project_metrics(project),
    }

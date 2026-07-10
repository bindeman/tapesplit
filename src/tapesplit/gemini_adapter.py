from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from tapesplit.costs import ApiUsage, append_api_usage, estimate_api_cost_usd
from tapesplit.env import load_dotenv
from tapesplit.media import ffprobe_video
from tapesplit.storage import append_jsonl, read_jsonl, write_json


@dataclass(frozen=True)
class GeminiConfig:
    use_vertex: bool
    project: str | None
    location: str
    model: str
    media_resolution: str
    default_fps: float
    gcs_bucket: str | None
    project_budget_usd: float
    thinking_budget: int | None

    @property
    def configured(self) -> bool:
        return bool(self.use_vertex and self.project and self.location and self.model)


@dataclass(frozen=True)
class GeminiChunk:
    index: int
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


DEFAULT_GEMINI_MAX_UPLOAD_BYTES = 1_450_000_000


def load_gemini_config(env_path: Path | None = None) -> GeminiConfig:
    load_dotenv(env_path)
    model = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    return GeminiConfig(
        use_vertex=_truthy(os.environ.get("GEMINI_USE_VERTEX"), default=True),
        project=_empty_to_none(os.environ.get("GOOGLE_CLOUD_PROJECT")),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1",
        model=model,
        media_resolution=(os.environ.get("GEMINI_MEDIA_RESOLUTION") or "low").lower(),
        default_fps=float(os.environ.get("GEMINI_DEFAULT_FPS") or 1),
        gcs_bucket=_empty_to_none(os.environ.get("GEMINI_GCS_BUCKET")),
        project_budget_usd=float(os.environ.get("GEMINI_PROJECT_BUDGET_USD") or 10),
        thinking_budget=_gemini_thinking_budget(model),
    )


def check_gemini_config(env_path: Path | None = None) -> dict[str, Any]:
    config = load_gemini_config(env_path)
    return {
        "gemini_use_vertex": config.use_vertex,
        "gemini_project": config.project,
        "gemini_location": config.location,
        "gemini_model": config.model,
        "gemini_media_resolution": config.media_resolution,
        "gemini_default_fps": config.default_fps,
        "gemini_gcs_bucket": bool(config.gcs_bucket),
        "gemini_thinking_budget": config.thinking_budget,
        "gemini_configured": config.configured,
        "gemini_adc": _has_adc(),
    }


def estimate_video_analysis(
    *,
    duration_s: float,
    model: str | None = None,
    fps: float = 1,
    media_resolution: str = "low",
    output_tokens: int = 6000,
) -> dict[str, Any]:
    config = load_gemini_config()
    resolved_model = model or config.model
    units = estimate_video_token_units(
        duration_s=duration_s,
        fps=fps,
        media_resolution=media_resolution,
        output_tokens=output_tokens,
    )
    return {
        "provider": "google_vertex",
        "service": f"generate_content:{resolved_model}",
        "duration_s": round(duration_s, 3),
        "fps": fps,
        "media_resolution": media_resolution,
        "units": units,
        "estimated_cost_usd": estimate_api_cost_usd(
            provider="google_vertex",
            service=f"generate_content:{resolved_model}",
            units=units,
        ),
    }


def estimate_project_video(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    fps: float | None = None,
    media_resolution: str | None = None,
    output_tokens: int = 6000,
    chunk_seconds: float | None = None,
    chunk_overlap_seconds: float = 0.0,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    tape = _select_tape(tapes, source_video_id)
    config = load_gemini_config()
    duration_s = float((tape.get("probe") or {}).get("duration_s") or 0.0)
    if chunk_seconds is None:
        estimate = estimate_video_analysis(
            duration_s=duration_s,
            model=config.model,
            fps=fps if fps is not None else config.default_fps,
            media_resolution=media_resolution or config.media_resolution,
            output_tokens=output_tokens,
        )
    else:
        estimate = estimate_chunked_video_analysis(
            duration_s=duration_s,
            model=config.model,
            fps=fps if fps is not None else config.default_fps,
            media_resolution=media_resolution or config.media_resolution,
            output_tokens_per_chunk=output_tokens,
            chunk_seconds=chunk_seconds,
            overlap_seconds=chunk_overlap_seconds,
        )
    estimate.update(
        {
            "project": str(project),
            "source_video_id": tape.get("id"),
            "filename": tape.get("filename"),
        }
    )
    return estimate


def estimate_project_videos(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    all_videos: bool = False,
    uploaded_to_twelvelabs_only: bool = False,
    fps: float | None = None,
    media_resolution: str | None = None,
    output_tokens: int = 6000,
    chunk_seconds: float | None = None,
    chunk_overlap_seconds: float = 0.0,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    selected_tapes = _select_tapes(
        project,
        tapes,
        source_video_id=source_video_id,
        all_videos=all_videos,
        uploaded_to_twelvelabs_only=uploaded_to_twelvelabs_only,
    )
    config = load_gemini_config()
    results = []
    for tape in selected_tapes:
        duration_s = float((tape.get("probe") or {}).get("duration_s") or 0.0)
        if chunk_seconds is None:
            estimate = estimate_video_analysis(
                duration_s=duration_s,
                model=config.model,
                fps=fps if fps is not None else config.default_fps,
                media_resolution=media_resolution or config.media_resolution,
                output_tokens=output_tokens,
            )
        else:
            estimate = estimate_chunked_video_analysis(
                duration_s=duration_s,
                model=config.model,
                fps=fps if fps is not None else config.default_fps,
                media_resolution=media_resolution or config.media_resolution,
                output_tokens_per_chunk=output_tokens,
                chunk_seconds=chunk_seconds,
                overlap_seconds=chunk_overlap_seconds,
            )
        estimate.update(
            {
                "project": str(project),
                "source_video_id": tape.get("id"),
                "filename": tape.get("filename"),
            }
        )
        results.append(estimate)

    known_cost = 0.0
    unknown_cost_estimates = 0
    for result in results:
        cost = result.get("estimated_cost_usd")
        if cost is None:
            unknown_cost_estimates += 1
        else:
            known_cost += float(cost)

    return {
        "project": str(project),
        "videos": len(results),
        "all_videos": all_videos,
        "uploaded_to_twelvelabs_only": uploaded_to_twelvelabs_only,
        "total_duration_s": round(sum(float(result.get("duration_s") or 0.0) for result in results), 3),
        "units": _sum_estimate_units(results),
        "known_estimated_cost_usd": round(known_cost, 8),
        "unknown_cost_estimates": unknown_cost_estimates,
        "total_estimated_cost_usd": None if unknown_cost_estimates else round(known_cost, 8),
        "results": results,
    }


def estimate_chunked_video_analysis(
    *,
    duration_s: float,
    model: str | None = None,
    fps: float = 1,
    media_resolution: str = "low",
    output_tokens_per_chunk: int = 6000,
    chunk_seconds: float,
    overlap_seconds: float = 0.0,
) -> dict[str, Any]:
    config = load_gemini_config()
    resolved_model = model or config.model
    chunks = plan_video_chunks(
        duration_s=duration_s,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
    )
    return estimate_chunks_analysis(
        chunks=chunks,
        model=resolved_model,
        duration_s=duration_s,
        fps=fps,
        media_resolution=media_resolution,
        output_tokens_per_chunk=output_tokens_per_chunk,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
    )


def estimate_chunks_analysis(
    *,
    chunks: list[GeminiChunk],
    model: str,
    duration_s: float,
    fps: float,
    media_resolution: str,
    output_tokens_per_chunk: int,
    chunk_seconds: float,
    overlap_seconds: float,
) -> dict[str, Any]:
    total_units: dict[str, float | int] = {
        "input_text_tokens": 0,
        "input_video_tokens": 0,
        "input_audio_tokens": 0,
        "output_tokens": 0,
    }
    chunk_estimates = []
    for chunk in chunks:
        units = estimate_video_token_units(
            duration_s=chunk.duration_s,
            fps=fps,
            media_resolution=media_resolution,
            output_tokens=output_tokens_per_chunk,
        )
        for key, value in units.items():
            total_units[key] = float(total_units.get(key, 0)) + float(value)
        chunk_estimates.append(
            {
                "chunk_id": _chunk_id(chunk.index),
                "chunk_index": chunk.index,
                "start_s": chunk.start_s,
                "end_s": chunk.end_s,
                "duration_s": round(chunk.duration_s, 3),
                "units": units,
                "estimated_cost_usd": estimate_api_cost_usd(
                    provider="google_vertex",
                    service=f"generate_content:{model}",
                    units=units,
                ),
            }
        )
    rounded_units = {
        key: int(round(value)) if key.endswith("_tokens") else value
        for key, value in total_units.items()
    }
    return {
        "provider": "google_vertex",
        "service": f"generate_content:{model}",
        "duration_s": round(duration_s, 3),
        "fps": fps,
        "media_resolution": media_resolution,
        "chunk_seconds": chunk_seconds,
        "chunk_overlap_seconds": overlap_seconds,
        "chunks": len(chunks),
        "analyzed_duration_s": round(sum(chunk.duration_s for chunk in chunks), 3),
        "units": rounded_units,
        "estimated_cost_usd": estimate_api_cost_usd(
            provider="google_vertex",
            service=f"generate_content:{model}",
            units=rounded_units,
        ),
        "chunk_estimates": chunk_estimates,
    }


def plan_video_chunks(
    *,
    duration_s: float,
    chunk_seconds: float,
    overlap_seconds: float = 0.0,
) -> list[GeminiChunk]:
    if duration_s <= 0:
        return []
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be greater than 0")
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must be greater than or equal to 0")
    if overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must be smaller than chunk_seconds")

    chunks = []
    start_s = 0.0
    index = 1
    step_s = chunk_seconds - overlap_seconds
    while start_s < duration_s:
        end_s = min(duration_s, start_s + chunk_seconds)
        chunks.append(GeminiChunk(index=index, start_s=round(start_s, 3), end_s=round(end_s, 3)))
        if end_s >= duration_s:
            break
        start_s += step_s
        index += 1
    return chunks


def estimate_video_token_units(
    *,
    duration_s: float,
    fps: float,
    media_resolution: str,
    output_tokens: int = 6000,
) -> dict[str, float | int]:
    if fps <= 0:
        raise ValueError("fps must be greater than 0")
    frame_tokens = 66 if media_resolution.lower() in {"low", "medium"} else 258
    return {
        "input_text_tokens": 1200,
        "input_video_tokens": round(duration_s * fps * frame_tokens),
        "input_audio_tokens": round(duration_s * 32),
        "output_tokens": output_tokens,
    }


def prepare_project_video_proxy(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    enabled: bool = True,
    force: bool = False,
    max_upload_bytes: int | None = None,
    target_height: int = 480,
    target_fps: float = 12.0,
    audio_bitrate_kbps: int = 96,
) -> dict[str, Any]:
    load_dotenv()
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    tape = _select_tape(tapes, source_video_id)
    video_path = Path(tape["path"]).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video no longer exists: {video_path}")

    resolved_max_bytes = _gemini_max_upload_bytes(max_upload_bytes)
    source_size = video_path.stat().st_size
    probe = tape.get("probe") or ffprobe_video(video_path)
    duration_s = float(probe.get("duration_s") or 0.0)
    if not enabled or (source_size <= resolved_max_bytes and not force):
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_video_id": tape["id"],
            "filename": tape.get("filename"),
            "source_path": str(video_path),
            "analysis_path": str(video_path),
            "proxy_path": "",
            "proxy_created": False,
            "proxy_reason": "disabled" if not enabled else "source_under_limit",
            "duration_s": round(duration_s, 3),
            "source_size_bytes": source_size,
            "analysis_size_bytes": source_size,
            "max_upload_bytes": resolved_max_bytes,
            "under_limit": source_size <= resolved_max_bytes,
        }
        append_jsonl(project / "gemini_video_proxies.jsonl", record)
        return record

    if duration_s <= 0:
        raise ValueError(f"cannot prepare Gemini proxy without duration: {video_path}")

    profile = _proxy_encoding_profile(
        duration_s=duration_s,
        max_upload_bytes=resolved_max_bytes,
        target_height=target_height,
        target_fps=target_fps,
        audio_bitrate_kbps=audio_bitrate_kbps,
    )
    proxy_dir = project / "gemini_proxies" / str(tape["id"])
    proxy_dir.mkdir(parents=True, exist_ok=True)
    output = proxy_dir / (
        f"{tape['id']}_whole_"
        f"h{profile['height']}_fps{str(profile['fps']).replace('.', 'p')}_"
        f"{int(resolved_max_bytes / 1_000_000)}mb.mp4"
    )
    if force or not output.exists() or output.stat().st_size > resolved_max_bytes:
        _create_video_proxy(video_path, output, profile=profile)

    output_size = output.stat().st_size
    if output_size > resolved_max_bytes:
        tighter_profile = _proxy_encoding_profile(
            duration_s=duration_s,
            max_upload_bytes=int(resolved_max_bytes * 0.82),
            target_height=min(target_height, 360),
            target_fps=min(target_fps, 8.0),
            audio_bitrate_kbps=min(audio_bitrate_kbps, 64),
        )
        _create_video_proxy(video_path, output, profile=tighter_profile)
        profile = tighter_profile
        output_size = output.stat().st_size

    if output_size > resolved_max_bytes:
        raise RuntimeError(
            f"Gemini proxy is still over upload limit: {output_size} > {resolved_max_bytes} bytes ({output})"
        )

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_video_id": tape["id"],
        "filename": tape.get("filename"),
        "source_path": str(video_path),
        "analysis_path": str(output),
        "proxy_path": str(output),
        "proxy_created": True,
        "proxy_reason": "source_over_limit" if source_size > resolved_max_bytes else "forced",
        "duration_s": round(duration_s, 3),
        "source_size_bytes": source_size,
        "analysis_size_bytes": output_size,
        "max_upload_bytes": resolved_max_bytes,
        "under_limit": output_size <= resolved_max_bytes,
        "encoding_profile": profile,
    }
    append_jsonl(project / "gemini_video_proxies.jsonl", record)
    return record


def prepare_project_video_proxies(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    enabled: bool = True,
    force: bool = False,
    max_upload_bytes: int | None = None,
    target_height: int = 480,
    target_fps: float = 12.0,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if source_video_id:
        tapes = [_select_tape(tapes, source_video_id)]
    results = [
        prepare_project_video_proxy(
            project,
            source_video_id=str(tape.get("id") or ""),
            enabled=enabled,
            force=force,
            max_upload_bytes=max_upload_bytes,
            target_height=target_height,
            target_fps=target_fps,
        )
        for tape in tapes
    ]
    return {
        "project": str(project),
        "videos": len(results),
        "proxies_created": sum(1 for result in results if result.get("proxy_created")),
        "under_limit": all(result.get("under_limit") for result in results),
        "max_upload_bytes": _gemini_max_upload_bytes(max_upload_bytes),
        "results": results,
    }


def _proxy_encoding_profile(
    *,
    duration_s: float,
    max_upload_bytes: int,
    target_height: int,
    target_fps: float,
    audio_bitrate_kbps: int,
) -> dict[str, Any]:
    usable_kbps = max(1.0, (max_upload_bytes * 8 * 0.9) / max(duration_s, 1.0) / 1000)
    audio_kbps = min(audio_bitrate_kbps, max(48, int(usable_kbps * 0.18)))
    video_kbps = max(250, int(usable_kbps - audio_kbps))
    return {
        "height": max(180, int(target_height)),
        "fps": max(4.0, float(target_fps)),
        "video_bitrate_kbps": video_kbps,
        "audio_bitrate_kbps": audio_kbps,
    }


def _create_video_proxy(video_path: Path, output: Path, *, profile: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    video_kbps = int(profile["video_bitrate_kbps"])
    audio_kbps = int(profile["audio_bitrate_kbps"])
    fps = float(profile["fps"])
    height = int(profile["height"])
    vf = f"scale=-2:{height}:flags=bicubic,fps={fps:g}"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{max(video_kbps, int(video_kbps * 1.4))}k",
        "-bufsize",
        f"{max(video_kbps * 2, 500)}k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=7200)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for Gemini whole-video proxy preparation") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"ffmpeg failed creating Gemini proxy: {detail}") from exc


def smoke_test(project_dir: Path | None = None) -> dict[str, Any]:
    config = load_gemini_config()
    if not config.configured:
        raise RuntimeError("Gemini Vertex config is incomplete")
    response = generate_content(
        contents=[
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            'Return only compact JSON: {"ok": true, '
                            '"provider": "vertex_gemini"}.'
                        )
                    }
                ],
            }
        ],
        config=config,
        operation="smoke_test",
        project_dir=project_dir,
        max_output_tokens=64,
    )
    return {
        "project": config.project,
        "location": config.location,
        "model": config.model,
        "text": _response_text(response),
        "usage": response.get("usageMetadata"),
    }


def analyze_project_video(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    analysis_run_id: str | None = None,
    fps: float | None = None,
    media_resolution: str | None = None,
    max_output_tokens: int = 8000,
    force_upload: bool = False,
    use_proxy: bool = True,
    force_proxy: bool = False,
    max_upload_bytes: int | None = None,
    proxy_height: int = 480,
    proxy_fps: float = 12.0,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    tape = _select_tape(tapes, source_video_id)
    video_path = Path(tape["path"]).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video no longer exists: {video_path}")

    config = load_gemini_config()
    if not config.configured:
        raise RuntimeError("Gemini Vertex config is incomplete")
    if not config.gcs_bucket:
        raise RuntimeError("GEMINI_GCS_BUCKET is required for Vertex video analysis")

    resolved_fps = fps if fps is not None else config.default_fps
    resolved_media_resolution = media_resolution or config.media_resolution
    resolved_analysis_run_id = analysis_run_id or (
        f"gem_video_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    )
    probe = tape.get("probe") or ffprobe_video(video_path)
    duration_s = float(probe.get("duration_s") or 0.0)
    prepared_video = prepare_project_video_proxy(
        project,
        source_video_id=tape["id"],
        enabled=use_proxy,
        force=force_proxy,
        max_upload_bytes=max_upload_bytes,
        target_height=proxy_height,
        target_fps=proxy_fps,
    )
    analysis_video_path = Path(prepared_video["analysis_path"]).expanduser().resolve()
    estimate = estimate_video_analysis(
        duration_s=duration_s,
        model=config.model,
        fps=resolved_fps,
        media_resolution=resolved_media_resolution,
        output_tokens=max_output_tokens,
    )
    _enforce_budget(project, config, estimate)

    upload = upload_video_to_gcs(
        project_dir=project,
        video_path=analysis_video_path,
        source_video_id=tape["id"],
        bucket=config.gcs_bucket,
        location=config.location,
        cloud_project=config.project or "",
        force=force_upload,
        metadata={
            "upload_role": "whole_tape_analysis",
            "source_video_path": str(video_path),
            "prepared_video": prepared_video,
        },
    )
    prompt = _analysis_prompt(duration_s=duration_s)
    response = generate_content(
        contents=[
            {
                "role": "user",
                "parts": [
                    {
                        "fileData": {
                            "mimeType": _mime_type(analysis_video_path),
                            "fileUri": upload["gcs_uri"],
                        },
                        "videoMetadata": {"fps": resolved_fps},
                    },
                    {"text": prompt},
                ],
            }
        ],
        config=config,
        operation="video_analysis",
        project_dir=project,
        source_video_id=tape["id"],
        gcs_uri=upload["gcs_uri"],
        media_resolution=resolved_media_resolution,
        fps=resolved_fps,
        max_output_tokens=max_output_tokens,
    )
    text = _response_text(response)
    raw_record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "analysis_run_id": resolved_analysis_run_id,
        "source_video_id": tape["id"],
        "filename": tape.get("filename"),
        "time_basis": "source_video",
        "gcs_uri": upload["gcs_uri"],
        "analysis_video_path": str(analysis_video_path),
        "prepared_video": prepared_video,
        "model": config.model,
        "location": config.location,
        "fps": resolved_fps,
        "media_resolution": resolved_media_resolution,
        "usage": response.get("usageMetadata"),
        "response_id": response.get("responseId"),
        "finish_reason": ((response.get("candidates") or [{}])[0]).get("finishReason"),
        "text": text,
    }
    write_json(project / "gemini_response.raw.json", raw_record)
    try:
        parsed = parse_json_object(text)
    except json.JSONDecodeError as exc:
        append_jsonl(
            project / "gemini_analysis_errors.jsonl",
            {
                **{key: value for key, value in raw_record.items() if key != "text"},
                "error": str(exc),
            },
        )
        raise RuntimeError(
            f"Gemini returned malformed JSON for {tape['id']}; raw response saved to "
            f"{project / 'gemini_response.raw.json'}"
        ) from exc
    record = {
        **{key: value for key, value in raw_record.items() if key != "text"},
        "analysis": parsed,
    }
    append_jsonl(project / "gemini_analyses.jsonl", record)
    write_json(project / "gemini_analysis.raw.json", record)
    return {
        "project": str(project),
        "analysis_run_id": resolved_analysis_run_id,
        "source_video_id": tape["id"],
        "model": config.model,
        "gcs_uri": upload["gcs_uri"],
        "analysis_video_path": str(analysis_video_path),
        "prepared_video": prepared_video,
        "fps": resolved_fps,
        "media_resolution": resolved_media_resolution,
        "estimated_cost_usd": estimate["estimated_cost_usd"],
        "usage": response.get("usageMetadata"),
        "analysis_keys": sorted(parsed.keys()),
    }


def analyze_project_videos(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    all_videos: bool = False,
    uploaded_to_twelvelabs_only: bool = False,
    continue_on_error: bool = False,
    fps: float | None = None,
    media_resolution: str | None = None,
    max_output_tokens: int = 8000,
    force_upload: bool = False,
    use_proxy: bool = True,
    force_proxy: bool = False,
    max_upload_bytes: int | None = None,
    proxy_height: int = 480,
    proxy_fps: float = 12.0,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    selected_tapes = _select_tapes(
        project,
        tapes,
        source_video_id=source_video_id,
        all_videos=all_videos,
        uploaded_to_twelvelabs_only=uploaded_to_twelvelabs_only,
    )
    analysis_run_id = f"gem_video_batch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    results = []
    errors = []
    for tape in selected_tapes:
        try:
            results.append(
                analyze_project_video(
                    project,
                    source_video_id=str(tape.get("id") or ""),
                    analysis_run_id=analysis_run_id,
                    fps=fps,
                    media_resolution=media_resolution,
                    max_output_tokens=max_output_tokens,
                    force_upload=force_upload,
                    use_proxy=use_proxy,
                    force_proxy=force_proxy,
                    max_upload_bytes=max_upload_bytes,
                    proxy_height=proxy_height,
                    proxy_fps=proxy_fps,
                )
            )
        except Exception as exc:
            error = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_video_id": tape.get("id"),
                "filename": tape.get("filename"),
                "operation": "video_analysis_batch",
                "analysis_run_id": analysis_run_id,
                "error": str(exc),
            }
            append_jsonl(project / "gemini_analysis_errors.jsonl", error)
            errors.append(error)
            if not continue_on_error:
                raise

    return {
        "project": str(project),
        "analysis_run_id": analysis_run_id,
        "videos_requested": len(selected_tapes),
        "videos_completed": len(results),
        "errors": errors,
        "results": results,
    }


def analyze_project_video_chunks(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    fps: float | None = None,
    media_resolution: str | None = None,
    max_output_tokens: int = 8000,
    chunk_seconds: float = 900.0,
    chunk_overlap_seconds: float = 15.0,
    run_id: str | None = None,
    start_chunk: int | None = None,
    limit_chunks: int | None = None,
    force_clips: bool = False,
    force_upload: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    tape = _select_tape(tapes, source_video_id)
    video_path = Path(tape["path"]).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video no longer exists: {video_path}")

    config = load_gemini_config()
    if not config.configured:
        raise RuntimeError("Gemini Vertex config is incomplete")
    if not config.gcs_bucket:
        raise RuntimeError("GEMINI_GCS_BUCKET is required for Vertex video analysis")

    resolved_fps = fps if fps is not None else config.default_fps
    resolved_media_resolution = media_resolution or config.media_resolution
    probe = tape.get("probe") or ffprobe_video(video_path)
    duration_s = float(probe.get("duration_s") or 0.0)
    if start_chunk is not None and start_chunk <= 0:
        raise ValueError("start_chunk must be greater than 0")
    if limit_chunks is not None and limit_chunks <= 0:
        raise ValueError("limit_chunks must be greater than 0")
    all_chunks = plan_video_chunks(
        duration_s=duration_s,
        chunk_seconds=chunk_seconds,
        overlap_seconds=chunk_overlap_seconds,
    )
    chunks = [chunk for chunk in all_chunks if start_chunk is None or chunk.index >= start_chunk]
    if limit_chunks is not None:
        chunks = chunks[:limit_chunks]
    estimate = estimate_chunks_analysis(
        chunks=chunks,
        model=config.model,
        duration_s=duration_s,
        fps=resolved_fps,
        media_resolution=resolved_media_resolution,
        output_tokens_per_chunk=max_output_tokens,
        chunk_seconds=chunk_seconds,
        overlap_seconds=chunk_overlap_seconds,
    )
    if start_chunk is not None:
        estimate["start_chunk"] = start_chunk
    if limit_chunks is not None:
        estimate["limited_from_chunks"] = len(all_chunks)
    _enforce_budget(project, config, estimate)

    analysis_run_id = run_id or f"gem_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    raw_dir = project / "gemini_raw" / analysis_run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for chunk in chunks:
        chunk_id = _chunk_id(chunk.index)
        # The uploaded file is the excerpt, so the prompt must describe the
        # excerpt's duration; announcing the full tape length here licenses
        # timelines that overrun the clip.
        chunk_prompt = _chunk_prompt(_analysis_prompt(duration_s=chunk.duration_s), chunk)
        clip_path = create_video_chunk(
            project_dir=project,
            video_path=video_path,
            source_video_id=tape["id"],
            chunk=chunk,
            force=force_clips,
        )
        upload = upload_video_to_gcs(
            project_dir=project,
            video_path=clip_path,
            source_video_id=tape["id"],
            bucket=config.gcs_bucket,
            location=config.location,
            cloud_project=config.project or "",
            force=force_upload,
            metadata={
                "analysis_run_id": analysis_run_id,
                "chunk_id": chunk_id,
                "chunk_index": chunk.index,
                "chunk_start_s": chunk.start_s,
                "chunk_end_s": chunk.end_s,
                "source_video_path": str(video_path),
            },
        )
        response = generate_content(
            contents=[
                {
                    "role": "user",
                    "parts": [
                        {
                            "fileData": {
                                "mimeType": _mime_type(clip_path),
                                "fileUri": upload["gcs_uri"],
                            },
                            "videoMetadata": {"fps": resolved_fps},
                        },
                        {"text": chunk_prompt},
                    ],
                }
            ],
            config=config,
            operation="video_chunk_analysis",
            project_dir=project,
            source_video_id=tape["id"],
            gcs_uri=upload["gcs_uri"],
            media_resolution=resolved_media_resolution,
            fps=resolved_fps,
            max_output_tokens=max_output_tokens,
            metadata={
                "analysis_run_id": analysis_run_id,
                "chunk_id": chunk_id,
                "chunk_index": chunk.index,
                "chunk_start_s": chunk.start_s,
                "chunk_end_s": chunk.end_s,
                "chunk_duration_s": round(chunk.duration_s, 3),
                "source_duration_s": round(duration_s, 3),
            },
        )
        text = _response_text(response)
        raw_record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "analysis_run_id": analysis_run_id,
            "source_video_id": tape["id"],
            "filename": tape.get("filename"),
            "chunk_id": chunk_id,
            "chunk_index": chunk.index,
            "chunk_start_s": chunk.start_s,
            "chunk_end_s": chunk.end_s,
            "chunk_duration_s": round(chunk.duration_s, 3),
            "time_basis": "chunk",
            "gcs_uri": upload["gcs_uri"],
            "local_clip_path": str(clip_path),
            "model": config.model,
            "location": config.location,
            "fps": resolved_fps,
            "media_resolution": resolved_media_resolution,
            "usage": response.get("usageMetadata"),
            "response_id": response.get("responseId"),
            "finish_reason": ((response.get("candidates") or [{}])[0]).get("finishReason"),
            "text": text,
        }
        write_json(raw_dir / f"{chunk_id}.response.raw.json", raw_record)
        try:
            parsed = parse_json_object(text)
        except json.JSONDecodeError as exc:
            append_jsonl(
                project / "gemini_analysis_errors.jsonl",
                {
                    **{key: value for key, value in raw_record.items() if key != "text"},
                    "error": str(exc),
                },
            )
            raise RuntimeError(
                f"Gemini returned malformed JSON for {chunk_id}; raw response saved to "
                f"{raw_dir / f'{chunk_id}.response.raw.json'}"
            ) from exc
        record = {
            **{key: value for key, value in raw_record.items() if key != "text"},
            "analysis": parsed,
        }
        append_jsonl(project / "gemini_analyses.jsonl", record)
        write_json(raw_dir / f"{chunk_id}.analysis.raw.json", record)
        results.append(
            {
                "chunk_id": chunk_id,
                "chunk_index": chunk.index,
                "chunk_start_s": chunk.start_s,
                "chunk_end_s": chunk.end_s,
                "gcs_uri": upload["gcs_uri"],
                "usage": response.get("usageMetadata"),
                "analysis_keys": sorted(parsed.keys()),
            }
        )

    run_records = _analysis_records_for_run(project, analysis_run_id)
    run_results = [
        {
            "chunk_id": record.get("chunk_id"),
            "chunk_index": record.get("chunk_index"),
            "chunk_start_s": record.get("chunk_start_s"),
            "chunk_end_s": record.get("chunk_end_s"),
            "gcs_uri": record.get("gcs_uri"),
            "usage": record.get("usage"),
            "analysis_keys": sorted((record.get("analysis") or {}).keys()),
        }
        for record in run_records
    ]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "analysis_run_id": analysis_run_id,
        "source_video_id": tape["id"],
        "filename": tape.get("filename"),
        "source_duration_s": round(duration_s, 3),
        "chunk_seconds": chunk_seconds,
        "chunk_overlap_seconds": chunk_overlap_seconds,
        "chunks_planned": len(all_chunks),
        "chunks_analyzed": len(run_results),
        "chunks_analyzed_this_invocation": len(results),
        "fps": resolved_fps,
        "media_resolution": resolved_media_resolution,
        "model": config.model,
        "estimated_cost_usd_this_invocation": estimate["estimated_cost_usd"],
        "results": run_results,
    }
    write_json(raw_dir / "manifest.json", manifest)
    write_json(project / "gemini_chunk_run.latest.json", manifest)
    return {
        "project": str(project),
        "analysis_run_id": analysis_run_id,
        "source_video_id": tape["id"],
        "model": config.model,
        "fps": resolved_fps,
        "media_resolution": resolved_media_resolution,
        "chunk_seconds": chunk_seconds,
        "chunk_overlap_seconds": chunk_overlap_seconds,
        "chunks_planned": len(all_chunks),
        "chunks_analyzed": len(run_results),
        "chunks_analyzed_this_invocation": len(results),
        "estimated_cost_usd_this_invocation": estimate["estimated_cost_usd"],
        "raw_dir": str(raw_dir),
    }


def create_video_chunk(
    *,
    project_dir: Path,
    video_path: Path,
    source_video_id: str,
    chunk: GeminiChunk,
    force: bool = False,
) -> Path:
    chunk_dir = project_dir / "gemini_clips" / source_video_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    output = chunk_dir / (
        f"{source_video_id}_{_chunk_id(chunk.index)}_"
        f"{int(round(chunk.start_s * 1000)):010d}_"
        f"{int(round(chunk.end_s * 1000)):010d}.mp4"
    )
    if output.exists() and not force:
        return output
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{chunk.start_s:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{chunk.duration_s:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for Gemini chunk analysis") from exc
    except subprocess.CalledProcessError:
        fallback_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{chunk.start_s:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{chunk.duration_s:.3f}",
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(output),
        ]
        try:
            subprocess.run(fallback_cmd, check=True, capture_output=True, text=True, timeout=7200)
        except subprocess.CalledProcessError as fallback_exc:
            detail = fallback_exc.stderr.strip() or fallback_exc.stdout.strip() or str(fallback_exc)
            raise RuntimeError(f"ffmpeg failed creating Gemini chunk: {detail}") from fallback_exc
    return output


def upload_video_to_gcs(
    *,
    project_dir: Path,
    video_path: Path,
    source_video_id: str,
    bucket: str,
    location: str,
    cloud_project: str,
    force: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_bucket(bucket=bucket, location=location, cloud_project=cloud_project)
    object_name = f"{project_dir.name}/{source_video_id}/{video_path.name}"
    gcs_uri = f"gs://{bucket}/{object_name}"
    exists = _gcloud_success(["gcloud", "storage", "ls", gcs_uri])
    if force or not exists:
        _run(
            [
                "gcloud",
                "storage",
                "cp",
                str(video_path),
                gcs_uri,
                "--project",
                cloud_project,
            ],
            timeout=3600,
        )
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_video_id": source_video_id,
        "path": str(video_path),
        "gcs_uri": gcs_uri,
        "size_bytes": video_path.stat().st_size,
        "uploaded": force or not exists,
        "metadata": metadata or {},
    }
    append_jsonl(project_dir / "gemini_uploads.jsonl", record)
    return record


def generate_content(
    *,
    contents: list[dict[str, Any]],
    config: GeminiConfig,
    operation: str,
    project_dir: Path | None = None,
    source_video_id: str | None = None,
    gcs_uri: str | None = None,
    media_resolution: str | None = None,
    fps: float | None = None,
    max_output_tokens: int = 8192,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    access_token = _access_token()
    url = (
        f"https://{config.location}-aiplatform.googleapis.com/v1/"
        f"projects/{config.project}/locations/{config.location}/publishers/google/"
        f"models/{config.model}:generateContent"
    )
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        },
    }
    if media_resolution:
        payload["generationConfig"]["mediaResolution"] = _media_resolution_enum(media_resolution)
    if config.thinking_budget is not None:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": config.thinking_budget}

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=900) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vertex Gemini request failed ({exc.code}): {detail}") from exc

    result = json.loads(body)
    if project_dir is not None:
        usage_units = usage_units_from_response(result)
        append_api_usage(
            project_dir,
            ApiUsage(
                provider="google_vertex",
                service=f"generate_content:{config.model}",
                operation=operation,
                units=usage_units,
                estimated_cost_usd=estimate_api_cost_usd(
                    provider="google_vertex",
                    service=f"generate_content:{config.model}",
                    units=usage_units,
                ),
                request_id=result.get("responseId"),
                metadata={
                    "source_video_id": source_video_id,
                    "gcs_uri": gcs_uri,
                    "model": config.model,
                    "location": config.location,
                    "media_resolution": media_resolution,
                    "fps": fps,
                    **(metadata or {}),
                },
            ),
        )
    return result


def usage_units_from_response(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usageMetadata") or {}
    units = {
        "input_text_tokens": 0,
        "input_video_tokens": 0,
        "input_audio_tokens": 0,
        "input_image_tokens": 0,
        "output_tokens": int(usage.get("candidatesTokenCount") or 0)
        + int(usage.get("thoughtsTokenCount") or 0),
    }
    for detail in usage.get("promptTokensDetails") or []:
        modality = str(detail.get("modality") or "").lower()
        token_count = int(detail.get("tokenCount") or 0)
        key = f"input_{modality}_tokens"
        if key in units:
            units[key] += token_count
        else:
            units["input_text_tokens"] += token_count
    return units


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return _loads_json_object(text)


def _loads_json_object(text: str) -> dict[str, Any]:
    candidates = [text]
    repaired = _normalize_bare_timestamp_values(text)
    if repaired != text:
        candidates.append(repaired)

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        sliced = text[start : end + 1]
        candidates.append(sliced)
        repaired_sliced = _normalize_bare_timestamp_values(sliced)
        if repaired_sliced != sliced:
            candidates.append(repaired_sliced)

    for candidate in list(candidates):
        salvaged = _salvage_truncated_json(candidate)
        if salvaged and salvaged not in candidates:
            candidates.append(salvaged)
            repaired_salvaged = _normalize_bare_timestamp_values(salvaged)
            if repaired_salvaged != salvaged and repaired_salvaged not in candidates:
                candidates.append(repaired_salvaged)

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("expected JSON object", text, 0)


def _salvage_truncated_json(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]
    stack: list[str] = []
    in_string = False
    escaped = False
    last_complete: tuple[int, list[str]] | None = None

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                break
            opener = stack[-1]
            if (opener, char) not in {("{", "}"), ("[", "]")}:
                break
            stack.pop()
            if not stack:
                return text[: index + 1]
            last_complete = (index + 1, stack.copy())
        elif char == "," and stack == ["{"]:
            last_complete = (index, stack.copy())

    if not last_complete:
        return None

    cut_index, cut_stack = last_complete
    prefix = text[:cut_index].rstrip()
    while prefix.endswith(","):
        prefix = prefix[:-1].rstrip()
    suffix = "".join("}" if opener == "{" else "]" for opener in reversed(cut_stack))
    return prefix + suffix


def _normalize_bare_timestamp_values(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group("key")
        parts = [int(part) for part in match.group("value").split(":")]
        if len(parts) == 2:
            seconds = parts[0] * 60 + parts[1]
        else:
            seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        return f'"{key}": {seconds}'

    return re.sub(
        r'"(?P<key>(?:start|end)_s)"\s*:\s*(?P<value>\d{1,2}:\d{2}(?::\d{2})?)(?=\s*[,}\]])',
        replace,
        text,
    )


def _analysis_prompt(*, duration_s: float | None = None) -> str:
    duration_instruction = ""
    if duration_s is not None and duration_s > 0:
        duration_instruction = (
            f"\nUploaded video duration: {duration_s:.3f} seconds "
            f"({_format_seconds(duration_s)}). All start_s and end_s values must be true "
            "elapsed seconds from the beginning of this uploaded video. For example, "
            "00:05:15 must be 315, not 5 or 51."
        )
    return (
        """
Analyze this digitized VHS/home-video transfer as an archival assistant.
Return only valid JSON. Use numeric seconds for all start_s and end_s values.
Do not use markdown. Do not include newline characters inside JSON strings.
Keep every string concise. Prefer broad ranges over exhaustive micro-events.
Hard cap tape_summary at 40 words and story at 80 words.
If a pattern repeats, summarize it once; do not repeat the same sentence.
"""
        + duration_instruction
        + """

Hard limits:
- scene_candidates: max 12
- event_candidates: max 8
- person_mentions: max 12
- place_candidates: max 8
- date_candidates: max 8
- language_segments: max 8
- non_content_ranges: max 10
- unrelated_ranges: max 8
- followup_segments: max 8
- evidence_text arrays: max 3 snippets, each under 80 characters

Goals:
- Identify family/home-video content versus unrelated movie/TV/noise content.
- Build event candidates with timestamps, titles, summaries, languages, people
  mentions, place candidates, date candidates, and confidence.
- Preserve uncertainty. Do not invent exact dates, identities, cities, schools,
  GPS coordinates, or relationships.
- Do not list every repeated classroom, performance, party, song, or cartoon
  beat. Collapse repeated material into one range with one concise label.
- Include evidence_text for each event: short quoted or paraphrased clues from
  audio/visual context that support the event.
- Do not repeat filler words, singing, animal sounds, applause, laughter, or
  long OCR/transcript fragments.
- Flag sections that need higher-fidelity follow-up, such as signs, visible
  dates, landmarks, fast transitions, or uncertain OCR.
- Treat metadata/export dates as digitization dates unless directly corroborated
  in the video.

Return this JSON shape:
{
  "tape_summary": "brief grounded summary",
  "story": "short narrative of the tape in order",
  "scene_candidates": [
    {
      "start_s": 0,
      "end_s": 0,
      "setting": "short setting",
      "description": "what is visible/audible",
      "relatedness": "likely_family|uncertain|likely_unrelated|non_content",
      "confidence": 0.0
    }
  ],
  "event_candidates": [
    {
      "title": "short event title",
      "start_s": 0,
      "end_s": 0,
      "event_type": "birthday|school|holiday|travel|home|medical|conversation|unknown",
      "summary": "grounded summary",
      "people": ["spoken or visible name candidates only"],
      "place_candidates": ["place clues, not final addresses"],
      "date_candidates": ["spoken/OCR date clues only"],
      "languages": ["language names"],
      "relatedness": "likely_family|uncertain|likely_unrelated",
      "evidence_text": ["short support snippets"],
      "confidence": 0.0,
      "needs_review": true
    }
  ],
  "person_mentions": [
    {"name": "name or alias", "start_s": 0, "end_s": 0, "evidence_text": "support", "confidence": 0.0}
  ],
  "place_candidates": [
    {"name": "candidate", "start_s": 0, "end_s": 0, "evidence_text": "support", "confidence": 0.0}
  ],
  "date_candidates": [
    {"value": "candidate text", "start_s": 0, "end_s": 0, "evidence_text": "support", "confidence": 0.0}
  ],
  "language_segments": [
    {"languages": ["language names"], "start_s": 0, "end_s": 0, "confidence": 0.0}
  ],
  "non_content_ranges": [
    {"label": "blue_screen|blank|static|tracking|silence", "start_s": 0, "end_s": 0, "confidence": 0.0}
  ],
  "unrelated_ranges": [
    {"reason": "movie_or_tv|credits|other", "start_s": 0, "end_s": 0, "confidence": 0.0}
  ],
  "uncertainties": ["important unknowns and possible errors"],
  "followup_segments": [
    {
      "start_s": 0,
      "end_s": 0,
      "reason": "why this needs another pass",
      "recommended_fps": 5,
      "recommended_media_resolution": "high"
    }
  ]
}
""".strip()
    )


def _chunk_prompt(base_prompt: str, chunk: GeminiChunk) -> str:
    # The excerpt-to-source mapping stays in request metadata only: naming
    # source seconds inside timestamp instructions is what taught models to
    # emit source-global (overrunning) timelines.
    return (
        f"{base_prompt}\n\n"
        "Chunk-specific instruction: analyze only this uploaded excerpt. "
        "All JSON start_s and end_s values must be seconds relative to the beginning "
        "of this excerpt, not the original tape. No start_s or end_s value may "
        f"exceed {chunk.duration_s:.0f}. Use stricter chunk limits: scene_candidates max 6, "
        "event_candidates max 4, person_mentions max 8, place_candidates max 6, "
        "date_candidates max 6, followup_segments max 4. Keep evidence_text arrays "
        "to at most 3 short snippets per event. Do not include transcript blocks, "
        "long quote lists, or duplicate evidence."
    )


def _chunk_id(index: int) -> str:
    return f"chunk_{index:04d}"


def _format_seconds(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _analysis_records_for_run(project: Path, analysis_run_id: str) -> list[dict[str, Any]]:
    return sorted(
        [
            row
            for row in read_jsonl(project / "gemini_analyses.jsonl")
            if row.get("analysis_run_id") == analysis_run_id
        ],
        key=lambda row: (float(row.get("chunk_start_s") or 0.0), int(row.get("chunk_index") or 0)),
    )


def _select_tape(tapes: list[dict[str, Any]], source_video_id: str | None) -> dict[str, Any]:
    if source_video_id is None:
        return tapes[0]
    for tape in tapes:
        if tape.get("id") == source_video_id:
            return tape
    raise ValueError(f"unknown source_video_id: {source_video_id}")


def _select_tapes(
    project: Path,
    tapes: list[dict[str, Any]],
    *,
    source_video_id: str | None,
    all_videos: bool,
    uploaded_to_twelvelabs_only: bool,
) -> list[dict[str, Any]]:
    if source_video_id and all_videos:
        raise ValueError("use either source_video_id or all_videos, not both")
    selected = list(tapes) if all_videos else [_select_tape(tapes, source_video_id)]
    if uploaded_to_twelvelabs_only:
        uploaded_source_ids = _twelvelabs_uploaded_source_ids(project)
        selected = [tape for tape in selected if tape.get("id") in uploaded_source_ids]
        if not selected:
            raise ValueError("no selected videos have TwelveLabs upload records")
    return selected


def _twelvelabs_uploaded_source_ids(project: Path) -> set[str]:
    rows = read_jsonl(project / "twelvelabs_assets.jsonl") + read_jsonl(project / "twelvelabs_tasks.jsonl")
    return {str(row.get("source_video_id")) for row in rows if row.get("source_video_id")}


def _sum_estimate_units(estimates: list[dict[str, Any]]) -> dict[str, float | int]:
    totals: dict[str, float | int] = {}
    for estimate in estimates:
        for key, value in (estimate.get("units") or {}).items():
            if isinstance(value, int):
                totals[key] = int(totals.get(key, 0)) + value
            elif isinstance(value, float):
                totals[key] = float(totals.get(key, 0.0)) + value
    return totals


def _enforce_budget(project: Path, config: GeminiConfig, estimate: dict[str, Any]) -> None:
    cost = estimate.get("estimated_cost_usd")
    if cost is None:
        raise RuntimeError("Gemini cost estimate is unknown; configure cost_rates before running video analysis")
    if float(cost) > config.project_budget_usd:
        raise RuntimeError(
            f"estimated Gemini call cost ${float(cost):.4f} exceeds configured "
            f"GEMINI_PROJECT_BUDGET_USD=${config.project_budget_usd:.2f}"
        )
    existing = 0.0
    for row in read_jsonl(project / "costs.api.jsonl"):
        if row.get("provider") == "google_vertex":
            existing += float(row.get("estimated_cost_usd") or 0.0)
    if existing + float(cost) > config.project_budget_usd:
        raise RuntimeError(
            f"estimated Gemini project spend ${existing + float(cost):.4f} exceeds "
            f"GEMINI_PROJECT_BUDGET_USD=${config.project_budget_usd:.2f}"
        )


def _ensure_bucket(*, bucket: str, location: str, cloud_project: str) -> None:
    uri = f"gs://{bucket}"
    if _gcloud_success(["gcloud", "storage", "buckets", "describe", uri, "--project", cloud_project]):
        return
    _run(
        [
            "gcloud",
            "storage",
            "buckets",
            "create",
            uri,
            "--project",
            cloud_project,
            "--location",
            location,
            "--uniform-bucket-level-access",
        ],
        timeout=120,
    )


def _response_text(response: dict[str, Any]) -> str:
    parts = (((response.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "\n".join(str(part.get("text") or "") for part in parts).strip()


def _media_resolution_enum(value: str) -> str:
    normalized = value.lower()
    if normalized in {"low", "medium", "high", "ultra_high"}:
        return f"MEDIA_RESOLUTION_{normalized.upper()}"
    if normalized.startswith("MEDIA_RESOLUTION_"):
        return normalized
    raise ValueError(f"unsupported media resolution: {value}")


def _gemini_thinking_budget(model: str) -> int | None:
    value = os.environ.get("GEMINI_THINKING_BUDGET")
    if value is not None and value.strip() != "":
        return int(value)
    if "2.5-flash" in model.lower():
        return 0
    return None


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".m4v"}:
        return "video/mp4"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix in {".mpg", ".mpeg"}:
        return "video/mpeg"
    return "video/mp4"


def _access_token() -> str:
    completed = _run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        timeout=30,
    )
    token = completed.stdout.strip()
    if not token:
        raise RuntimeError("could not get ADC access token")
    return token


def _has_adc() -> bool:
    return _gcloud_success(["gcloud", "auth", "application-default", "print-access-token"])


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("gcloud is required for Vertex Gemini operations") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{detail}") from exc


def _gcloud_success(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _gemini_max_upload_bytes(value: int | None = None) -> int:
    if value is not None:
        return int(value)
    env_value = os.environ.get("GEMINI_MAX_UPLOAD_BYTES")
    if env_value:
        return int(float(env_value))
    env_gb = os.environ.get("GEMINI_MAX_UPLOAD_GB")
    if env_gb:
        return int(float(env_gb) * 1_000_000_000)
    return DEFAULT_GEMINI_MAX_UPLOAD_BYTES


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None

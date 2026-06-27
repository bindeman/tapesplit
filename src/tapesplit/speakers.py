from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from tapesplit.env import load_dotenv
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.transcription import extract_project_audio


SUPPORTED_SPEAKER_FORMATS = {"json", "rttm"}
DEFAULT_SPEAKER_DIARIZATION_BACKEND = "pyannote"
DEFAULT_SPEAKER_DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"

_RTTM_RE = re.compile(r"\s+")


def check_speaker_diarization_config() -> dict[str, Any]:
    load_dotenv()
    return {
        "speaker_diarization_pyannote": _pyannote_available(),
        "speaker_diarization_hf_token": bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")),
        "speaker_diarization_default_backend": DEFAULT_SPEAKER_DIARIZATION_BACKEND,
        "speaker_diarization_default_model": DEFAULT_SPEAKER_DIARIZATION_MODEL,
    }


def diarize_project_speakers(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    backend: str = DEFAULT_SPEAKER_DIARIZATION_BACKEND,
    model_name: str = DEFAULT_SPEAKER_DIARIZATION_MODEL,
    force: bool = False,
) -> dict[str, Any]:
    load_dotenv()
    project = project_dir.expanduser().resolve()
    if backend != "pyannote":
        raise ValueError("speaker diarization backend must be pyannote")
    if not _pyannote_available():
        raise RuntimeError("pyannote.audio is not installed. Install a diarization extra or import RTTM/JSON instead.")

    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyannote.audio is not installed") from exc

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    pipeline = Pipeline.from_pretrained(model_name, use_auth_token=token)
    audio = extract_project_audio(project, source_video_id=source_video_id, force=False)
    run_id = f"spk_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    all_segments = []
    for item in audio["audio_files"]:
        diarization = pipeline(item["audio_path"])
        for turn, _track, speaker in diarization.itertracks(yield_label=True):
            all_segments.append(
                {
                    "source_video_id": item["source_video_id"],
                    "start_s": round(float(turn.start), 3),
                    "end_s": round(float(turn.end), 3),
                    "speaker_label": str(speaker),
                    "confidence": None,
                    "provider": "pyannote",
                    "model": model_name,
                    "metadata": {"run_id": run_id, "audio_path": item["audio_path"]},
                }
            )
    written = write_speaker_segments(project, all_segments, force=force, source_video_id=source_video_id)
    append_jsonl(
        project / "speaker_runs.jsonl",
        {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": "pyannote",
            "model": model_name,
            "source_video_id": source_video_id,
            "segments": written,
        },
    )
    return {
        "project": str(project),
        "run_id": run_id,
        "backend": backend,
        "model": model_name,
        "speaker_segments": written,
        "output": str(project / "speaker_segments.jsonl"),
    }


def import_speaker_segments(
    project_dir: Path,
    speaker_path: Path,
    *,
    source_video_id: str,
    speaker_format: str = "auto",
    offset_seconds: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    source_id = _validate_source_video_id(project, source_video_id)
    path = speaker_path.expanduser().resolve()
    segments = parse_speaker_file(path, speaker_format=speaker_format)
    for segment in segments:
        segment["source_video_id"] = source_id
        segment["start_s"] = round(float(segment.get("start_s") or 0.0) + offset_seconds, 3)
        segment["end_s"] = round(float(segment.get("end_s") or segment["start_s"]) + offset_seconds, 3)
        segment.setdefault("provider", "imported")
        segment.setdefault("model", "")
        segment.setdefault("metadata", {})
        segment["metadata"] = {**segment["metadata"], "source_file": str(path)}
    written = write_speaker_segments(project, segments, force=force, source_video_id=source_id)
    return {
        "project": str(project),
        "source_video_id": source_id,
        "speaker_segments": written,
        "output": str(project / "speaker_segments.jsonl"),
    }


def parse_speaker_file(path: Path, *, speaker_format: str = "auto") -> list[dict[str, Any]]:
    resolved = _resolve_speaker_format(path, speaker_format)
    if resolved == "json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("segments") if isinstance(payload, dict) else payload
        return [_normalize_speaker_segment(row) for row in rows or [] if isinstance(row, dict)]
    if resolved == "rttm":
        return _parse_rttm(path.read_text(encoding="utf-8"))
    raise ValueError(f"unsupported speaker format: {resolved}")


def write_speaker_segments(
    project: Path,
    segments: list[dict[str, Any]],
    *,
    force: bool,
    source_video_id: str | None = None,
) -> int:
    output = project / "speaker_segments.jsonl"
    existing = read_jsonl(output)
    normalized_new = [
        _normalize_speaker_segment(segment)
        for segment in segments
        if segment.get("source_video_id") and segment.get("speaker_label")
    ]
    new_source_ids = {row.get("source_video_id") for row in normalized_new if row.get("source_video_id")}
    if source_video_id and any(row.get("source_video_id") == source_video_id for row in existing) and not force:
        raise FileExistsError(
            f"speaker segments already exist for {source_video_id}; pass --force to replace them"
        )
    kept = [
        row
        for row in existing
        if row.get("source_video_id") not in (new_source_ids if not source_video_id else {source_video_id})
    ]
    rows = kept + normalized_new
    if output.exists():
        output.unlink()
    for index, row in enumerate(rows, start=1):
        append_jsonl(output, {"id": f"speaker_segment_{index:06d}", **row})
    return len(normalized_new)


def _parse_rttm(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        parts = _RTTM_RE.split(cleaned)
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        start_s = float(parts[3])
        duration_s = float(parts[4])
        rows.append(
            {
                "start_s": round(start_s, 3),
                "end_s": round(start_s + duration_s, 3),
                "speaker_label": parts[7],
                "confidence": None,
                "provider": "rttm",
                "model": "",
                "metadata": {"rttm_file_id": parts[1], "channel": parts[2]},
            }
        )
    return rows


def _normalize_speaker_segment(segment: dict[str, Any]) -> dict[str, Any]:
    start_s = _number_or_none(segment.get("start_s") or segment.get("start") or segment.get("begin")) or 0.0
    end_s = _number_or_none(segment.get("end_s") or segment.get("end"))
    if end_s is None:
        duration = _number_or_none(segment.get("duration_s") or segment.get("duration")) or 0.0
        end_s = start_s + duration
    speaker = segment.get("speaker_label") or segment.get("speaker") or segment.get("label")
    return {
        "source_video_id": str(segment.get("source_video_id") or ""),
        "start_s": round(float(start_s), 3),
        "end_s": round(float(end_s), 3),
        "speaker_label": str(speaker or ""),
        "confidence": segment.get("confidence"),
        "provider": segment.get("provider") or "imported",
        "model": segment.get("model") or "",
        "metadata": segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {},
        "review_status": segment.get("review_status") or "unreviewed",
    }


def _resolve_speaker_format(path: Path, requested: str) -> str:
    if requested != "auto":
        if requested not in SUPPORTED_SPEAKER_FORMATS:
            raise ValueError(f"unsupported speaker format: {requested}")
        return requested
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".rttm":
        return "rttm"
    raise ValueError(f"could not infer speaker format from {path.name}")


def _validate_source_video_id(project: Path, source_video_id: str) -> str:
    ids = {str(row.get("id")) for row in read_jsonl(project / "tapes.jsonl") if row.get("id")}
    if source_video_id not in ids:
        raise ValueError(f"source video id not found in project: {source_video_id}")
    return source_video_id


def _pyannote_available() -> bool:
    try:
        import pyannote.audio  # type: ignore  # noqa: F401
    except ImportError:
        return False
    return True


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

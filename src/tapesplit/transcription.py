from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from tapesplit.env import load_dotenv
from tapesplit.storage import append_jsonl, read_jsonl


SUPPORTED_TRANSCRIPT_FORMATS = {"json", "srt", "vtt"}


def check_transcription_config() -> dict[str, Any]:
    load_dotenv()
    return {
        "whisper_cli": shutil.which("whisper") is not None,
        "whisper_cpp_cli": shutil.which("whisper-cli") is not None,
        "whisper_cpp_main": shutil.which("main") is not None,
        "whisper_cpp_model": os.environ.get("WHISPER_CPP_MODEL") or "",
    }


def extract_project_audio(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    load_dotenv()
    project = project_dir.expanduser().resolve()
    audio_dir = project / "audio"
    audio_dir.mkdir(exist_ok=True)
    outputs = []
    for tape in _selected_tapes(project, source_video_id):
        output = audio_dir / f"{tape['id']}.wav"
        if output.exists() and not force:
            outputs.append({"source_video_id": tape["id"], "audio_path": str(output), "skipped": True})
            continue
        _run_ffmpeg_audio_extract(Path(tape["path"]), output)
        outputs.append({"source_video_id": tape["id"], "audio_path": str(output), "skipped": False})
    return {"project": str(project), "audio_files": outputs}


def transcribe_project_local(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    engine: str = "auto",
    model: str = "large-v3-turbo",
    model_path: Path | None = None,
    language: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    load_dotenv()
    project = project_dir.expanduser().resolve()
    selected_engine = _resolve_engine(engine, model_path=model_path)
    run_id = f"tr_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    transcript_dir = project / "transcript_raw" / run_id
    transcript_dir.mkdir(parents=True, exist_ok=True)

    audio_result = extract_project_audio(project, source_video_id=source_video_id, force=False)
    all_segments = []
    run_records = []
    for item in audio_result["audio_files"]:
        source_id = item["source_video_id"]
        audio_path = Path(item["audio_path"])
        output_path = _run_transcription_engine(
            engine=selected_engine,
            audio_path=audio_path,
            output_dir=transcript_dir,
            model=model,
            model_path=model_path,
            language=language,
        )
        segments = parse_transcript_file(output_path, default_language=language)
        for segment in segments:
            segment["source_video_id"] = source_id
            segment.setdefault("provider", selected_engine)
            segment.setdefault("model", str(model_path or model))
            segment.setdefault("metadata", {})
            segment["metadata"] = {
                **segment["metadata"],
                "run_id": run_id,
                "engine": selected_engine,
                "raw_output": str(output_path),
            }
        all_segments.extend(segments)
        run_records.append(
            {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_video_id": source_id,
                "engine": selected_engine,
                "model": str(model_path or model),
                "language": language or "auto",
                "audio_path": str(audio_path),
                "raw_output": str(output_path),
                "segments": len(segments),
            }
        )

    written = write_transcript_segments(project, all_segments, force=force, source_video_id=source_video_id)
    for record in run_records:
        append_jsonl(project / "transcript_runs.jsonl", record)
    return {
        "project": str(project),
        "run_id": run_id,
        "engine": selected_engine,
        "segments_written": written,
        "raw_dir": str(transcript_dir),
    }


def import_transcript(
    project_dir: Path,
    transcript_path: Path,
    *,
    source_video_id: str,
    transcript_format: str = "auto",
    language: str | None = None,
    offset_seconds: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    source_id = _validate_source_video_id(project, source_video_id)
    path = transcript_path.expanduser().resolve()
    segments = parse_transcript_file(path, transcript_format=transcript_format, default_language=language)
    for segment in segments:
        segment["source_video_id"] = source_id
        segment["start_s"] = round(float(segment.get("start_s") or 0.0) + offset_seconds, 3)
        segment["end_s"] = round(float(segment.get("end_s") or segment["start_s"]) + offset_seconds, 3)
        segment.setdefault("provider", "imported")
        segment.setdefault("model", "")
        segment.setdefault("metadata", {})
        segment["metadata"] = {**segment["metadata"], "source_file": str(path)}
    written = write_transcript_segments(project, segments, force=force, source_video_id=source_id)
    return {
        "project": str(project),
        "source_video_id": source_id,
        "segments_written": written,
        "output": str(project / "transcript_segments.jsonl"),
    }


def parse_transcript_file(
    path: Path,
    *,
    transcript_format: str = "auto",
    default_language: str | None = None,
) -> list[dict[str, Any]]:
    resolved_format = _resolve_transcript_format(path, transcript_format)
    if resolved_format == "json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _parse_json_transcript(payload, default_language=default_language)
    if resolved_format in {"srt", "vtt"}:
        return _parse_subtitle_transcript(path.read_text(encoding="utf-8"), default_language=default_language)
    raise ValueError(f"unsupported transcript format: {resolved_format}")


def write_transcript_segments(
    project: Path,
    segments: list[dict[str, Any]],
    *,
    force: bool,
    source_video_id: str | None = None,
) -> int:
    output = project / "transcript_segments.jsonl"
    existing = read_jsonl(output)
    if source_video_id and any(row.get("source_video_id") == source_video_id for row in existing) and not force:
        raise FileExistsError(
            f"transcript segments already exist for {source_video_id}; pass --force to replace them"
        )
    kept = [
        row
        for row in existing
        if not source_video_id or row.get("source_video_id") != source_video_id
    ]
    normalized_new = [_normalize_segment(segment) for segment in segments if _clean_text(segment.get("text"))]
    rows = kept + normalized_new
    if output.exists():
        output.unlink()
    for index, row in enumerate(rows, start=1):
        append_jsonl(output, {"id": f"tr_{index:06d}", **row})
    return len(normalized_new)


def _selected_tapes(project: Path, source_video_id: str | None) -> list[dict[str, Any]]:
    tapes = read_jsonl(project / "tapes.jsonl")
    if source_video_id:
        selected = [row for row in tapes if row.get("id") == source_video_id]
        if not selected:
            raise ValueError(f"unknown source_video_id: {source_video_id}")
        return selected
    return tapes


def _validate_source_video_id(project: Path, source_video_id: str) -> str:
    for tape in read_jsonl(project / "tapes.jsonl"):
        if tape.get("id") == source_video_id:
            return source_video_id
    raise ValueError(f"unknown source_video_id: {source_video_id}")


def _run_ffmpeg_audio_extract(video_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for audio extraction") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"ffmpeg audio extraction failed: {detail}") from exc


def _resolve_engine(engine: str, *, model_path: Path | None) -> str:
    if engine != "auto":
        if engine not in {"whisper", "whisper-cpp"}:
            raise ValueError("--engine must be one of: auto, whisper, whisper-cpp")
        return engine
    if (model_path or os.environ.get("WHISPER_CPP_MODEL")) and (shutil.which("whisper-cli") or shutil.which("main")):
        return "whisper-cpp"
    if shutil.which("whisper"):
        return "whisper"
    raise RuntimeError(
        "no local Whisper engine found. Install whisper.cpp/whisper-cli or openai-whisper, "
        "or import an existing JSON/SRT/VTT transcript with `tapesplit transcribe import`."
    )


def _run_transcription_engine(
    *,
    engine: str,
    audio_path: Path,
    output_dir: Path,
    model: str,
    model_path: Path | None,
    language: str | None,
) -> Path:
    if engine == "whisper-cpp":
        return _run_whisper_cpp(audio_path, output_dir, model_path=model_path, language=language)
    if engine == "whisper":
        return _run_whisper_cli(audio_path, output_dir, model=model, language=language)
    raise ValueError(f"unsupported engine: {engine}")


def _run_whisper_cpp(audio_path: Path, output_dir: Path, *, model_path: Path | None, language: str | None) -> Path:
    executable = shutil.which("whisper-cli") or shutil.which("main")
    if not executable:
        raise RuntimeError("whisper.cpp CLI not found; expected `whisper-cli` or `main` on PATH")
    model = model_path or Path(os.environ.get("WHISPER_CPP_MODEL", ""))
    if not str(model):
        raise RuntimeError("whisper.cpp requires --model-path or WHISPER_CPP_MODEL")
    output_base = output_dir / audio_path.stem
    cmd = [
        executable,
        "-m",
        str(model),
        "-f",
        str(audio_path),
        "-oj",
        "-of",
        str(output_base),
    ]
    cmd.extend(["-l", language or "auto"])
    _run_checked(cmd, "whisper.cpp transcription failed")
    return output_base.with_suffix(".json")


def _run_whisper_cli(audio_path: Path, output_dir: Path, *, model: str, language: str | None) -> Path:
    executable = shutil.which("whisper")
    if not executable:
        raise RuntimeError("openai-whisper CLI not found; expected `whisper` on PATH")
    cmd = [
        executable,
        str(audio_path),
        "--model",
        model,
        "--output_dir",
        str(output_dir),
        "--output_format",
        "json",
        "--verbose",
        "False",
    ]
    if language:
        cmd.extend(["--language", language])
    _run_checked(cmd, "Whisper transcription failed")
    return output_dir / f"{audio_path.stem}.json"


def _run_checked(cmd: list[str], error_prefix: str) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"{error_prefix}: {detail}") from exc


def _resolve_transcript_format(path: Path, requested: str) -> str:
    if requested != "auto":
        if requested not in SUPPORTED_TRANSCRIPT_FORMATS:
            raise ValueError(f"--format must be auto, json, srt, or vtt")
        return requested
    suffix = path.suffix.lower().lstrip(".")
    if suffix in SUPPORTED_TRANSCRIPT_FORMATS:
        return suffix
    raise ValueError(f"could not infer transcript format from {path.name}")


def _parse_json_transcript(payload: Any, *, default_language: str | None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_segments = payload
        language = default_language
    elif isinstance(payload, dict):
        raw_segments = payload.get("segments") or payload.get("transcription") or payload.get("results") or []
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        language = default_language or payload.get("language") or result.get("language")
    else:
        raw_segments = []
        language = default_language

    segments = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        start, end = _json_segment_times(raw)
        text = _clean_text(raw.get("text") or raw.get("transcript") or raw.get("sentence"))
        if not text:
            continue
        segments.append(
            {
                "start_s": start,
                "end_s": end,
                "text": text,
                "language": raw.get("language") or language or "unknown",
                "confidence": _segment_confidence(raw),
                "metadata": {"raw_segment": raw},
            }
        )
    return segments


def _json_segment_times(raw: dict[str, Any]) -> tuple[float, float]:
    if "start" in raw or "end" in raw:
        start = _float_or_zero(raw.get("start"))
        end = _float_or_zero(raw.get("end"), default=start)
        return round(start, 3), round(end, 3)
    offsets = raw.get("offsets") if isinstance(raw.get("offsets"), dict) else {}
    if offsets:
        start = _float_or_zero(offsets.get("from")) / 1000.0
        end = _float_or_zero(offsets.get("to"), default=offsets.get("from")) / 1000.0
        return round(start, 3), round(end, 3)
    timestamps = raw.get("timestamps") if isinstance(raw.get("timestamps"), dict) else {}
    if timestamps:
        start = _parse_timestamp(timestamps.get("from"))
        end = _parse_timestamp(timestamps.get("to"))
        return round(start, 3), round(end or start, 3)
    return 0.0, 0.0


def _parse_subtitle_transcript(text: str, *, default_language: str | None) -> list[dict[str, Any]]:
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"^WEBVTT.*?(?:\n\n|$)", "", normalized, flags=re.IGNORECASE | re.DOTALL)
    blocks = re.split(r"\n\s*\n", normalized.strip())
    segments = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        start_raw, end_raw = [part.strip() for part in lines[timing_index].split("-->", 1)]
        end_raw = end_raw.split()[0]
        content = " ".join(line for line in lines[timing_index + 1 :] if not line.isdigit())
        content = _clean_text(re.sub(r"<[^>]+>", "", content))
        if not content:
            continue
        start = _parse_timestamp(start_raw)
        end = _parse_timestamp(end_raw)
        segments.append(
            {
                "start_s": round(start, 3),
                "end_s": round(end or start, 3),
                "text": content,
                "language": default_language or "unknown",
                "confidence": 0.8,
                "metadata": {},
            }
        )
    return segments


def _normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    start = _float_or_zero(segment.get("start_s"))
    end = _float_or_zero(segment.get("end_s"), default=start)
    if end < start:
        start, end = end, start
    return {
        "source_video_id": segment.get("source_video_id"),
        "start_s": round(start, 3),
        "end_s": round(end, 3),
        "text": _clean_text(segment.get("text")) or "",
        "language": segment.get("language") or "unknown",
        "confidence": segment.get("confidence", 0.8),
        "provider": segment.get("provider") or "local",
        "model": segment.get("model") or "",
        "review_status": segment.get("review_status") or "unreviewed",
        "metadata": segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {},
    }


def _parse_timestamp(value: Any) -> float:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        return float(parts[0])
    except ValueError:
        return 0.0


def _segment_confidence(raw: dict[str, Any]) -> float:
    if isinstance(raw.get("confidence"), (int, float)):
        return float(raw["confidence"])
    if isinstance(raw.get("avg_logprob"), (int, float)):
        return max(0.0, min(1.0, 1.0 + float(raw["avg_logprob"])))
    if isinstance(raw.get("no_speech_prob"), (int, float)):
        return max(0.0, min(1.0, 1.0 - float(raw["no_speech_prob"])))
    return 0.8


def _float_or_zero(value: Any, *, default: Any = 0.0) -> float:
    if value in (None, ""):
        value = default
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None

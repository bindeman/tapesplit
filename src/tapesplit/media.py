from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable


VIDEO_EXTENSIONS = {
    ".avi",
    ".dv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".vob",
    ".wmv",
}


def iter_video_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            yield path
        return

    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
            yield child


def ffprobe_video(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is required. Install ffmpeg first.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"ffprobe failed for {path}: {detail}") from exc

    payload = json.loads(completed.stdout)
    format_info = payload.get("format", {})
    streams = payload.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]

    return {
        "duration_s": _float_or_none(format_info.get("duration") or video_stream.get("duration")),
        "size_bytes": _int_or_none(format_info.get("size")),
        "format_name": format_info.get("format_name"),
        "bit_rate": _int_or_none(format_info.get("bit_rate")),
        "video": {
            "codec": video_stream.get("codec_name"),
            "width": _int_or_none(video_stream.get("width")),
            "height": _int_or_none(video_stream.get("height")),
            "avg_frame_rate": video_stream.get("avg_frame_rate"),
            "field_order": video_stream.get("field_order"),
        },
        "audio_stream_count": len(audio_streams),
    }


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


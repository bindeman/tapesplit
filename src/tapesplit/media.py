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


def source_media_id(record: dict) -> str | None:
    """Read-compat shim for the v2 source_media_id rename (REFOUNDATION.md section 3).

    677 call sites key records by source_video_id; new code reads through this
    helper so producers can migrate field names module-by-module.
    """

    for key in ("source_media_id", "source_video_id", "media_id"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def build_media_index(project_dir: Path) -> dict:
    """Derive media.jsonl from tapes.jsonl; media-type rows other than tapes are kept.

    media.jsonl supersedes tapes.jsonl as the media-agnostic source registry
    (tape | photo | audio). Tapes remain authoritative during cutover: tape
    rows here are always regenerated from tapes.jsonl.
    """

    project = project_dir.expanduser().resolve()
    tapes_path = project / "tapes.jsonl"
    media_path = project / "media.jsonl"
    non_tape_rows = []
    if media_path.exists():
        for line in media_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("media_type") != "tape":
                non_tape_rows.append(row)
    tape_rows = []
    if tapes_path.exists():
        for line in tapes_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            tape = json.loads(line)
            tape_rows.append(
                {
                    "media_id": tape.get("id"),
                    "media_type": "tape",
                    "filename": tape.get("filename"),
                    "path": tape.get("path"),
                    "relative_path": tape.get("relative_path"),
                    "probe": tape.get("probe"),
                }
            )
    rows = tape_rows + non_tape_rows
    if rows:
        media_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    return {"media_rows": len(rows), "tape_rows": len(tape_rows), "output": str(media_path)}


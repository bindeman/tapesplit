from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from tapesplit.media import VIDEO_EXTENSIONS, ffprobe_video, iter_video_files
from tapesplit.storage import append_jsonl, write_json
from tapesplit.windows import make_windows


def default_project_path(input_path: Path) -> Path:
    path = input_path.expanduser().resolve()
    if path.is_dir():
        return path.with_name(f"{path.name}.tapesplit")
    return path.with_suffix(f"{path.suffix}.tapesplit")


def ingest(
    input_path: Path,
    out_path: Path | None = None,
    window_seconds: float = 30.0,
    force: bool = False,
) -> dict:
    source = input_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"input path does not exist: {source}")
    if window_seconds <= 0:
        raise ValueError("--window-seconds must be greater than 0")

    project = (out_path or default_project_path(source)).expanduser().resolve()
    if project.exists():
        if not force:
            raise FileExistsError(f"output exists, pass --force to overwrite: {project}")
        shutil.rmtree(project)

    project.mkdir(parents=True, exist_ok=True)
    (project / "keyframes").mkdir()
    (project / "thumbnails").mkdir()

    videos = list(iter_video_files(source))
    if not videos:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"no supported video files found in {source} ({supported})")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "window_seconds": window_seconds,
        "video_count": len(videos),
    }
    write_json(project / "manifest.json", manifest)

    total_windows = 0
    for index, video_path in enumerate(videos, start=1):
        video_id = f"video_{index:06d}"
        probe = ffprobe_video(video_path)
        duration = probe.get("duration_s") or 0.0
        record = {
            "id": video_id,
            "path": str(video_path),
            "filename": video_path.name,
            "relative_path": str(video_path.relative_to(source) if source.is_dir() else video_path.name),
            "probe": probe,
        }
        append_jsonl(project / "tapes.jsonl", record)

        for window in make_windows(video_id, duration, window_seconds):
            append_jsonl(project / "windows.jsonl", window)
            total_windows += 1

    return {
        "project": str(project),
        "videos": len(videos),
        "windows": total_windows,
        "window_seconds": window_seconds,
    }


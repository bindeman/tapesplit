from __future__ import annotations

from collections import defaultdict
import re
import subprocess
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_SCENE_THRESHOLD = 0.35
DEFAULT_MIN_SCENE_SECONDS = 1.0

_PTS_TIME_RE = re.compile(r"pts_time:([0-9.]+)")


def detect_scenes_for_project(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    min_scene_seconds: float = DEFAULT_MIN_SCENE_SECONDS,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1")
    if min_scene_seconds < 0:
        raise ValueError("min_scene_seconds must be greater than or equal to 0")

    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    selected = [tape for tape in tapes if source_video_id is None or tape.get("id") == source_video_id]
    if not selected:
        raise ValueError(f"source video not found: {source_video_id}")

    output_path = project / "scenes.jsonl"
    _remove_scene_rows(output_path, {str(tape["id"]) for tape in selected if tape.get("id")})
    non_content_by_source = _non_content_by_source(project)

    total_scenes = 0
    total_visual_cuts = 0
    by_source = {}
    for tape in selected:
        tape_id = str(tape["id"])
        duration_s = float((tape.get("probe") or {}).get("duration_s") or 0.0)
        video_path = Path(tape["path"])
        visual_cuts = detect_visual_cut_times(video_path, threshold=threshold)
        scenes = build_scene_intervals(
            source_video_id=tape_id,
            duration_s=duration_s,
            visual_cut_times=visual_cuts,
            non_content_ranges=non_content_by_source.get(tape_id, []),
            min_scene_seconds=min_scene_seconds,
        )
        for index, scene in enumerate(scenes, start=1):
            append_jsonl(
                output_path,
                {
                    "id": f"{tape_id}_scene_{index:06d}",
                    "source_video_id": tape_id,
                    "index": index,
                    **scene,
                },
            )
        total_scenes += len(scenes)
        total_visual_cuts += len(visual_cuts)
        by_source[tape_id] = {
            "filename": tape.get("filename"),
            "duration_s": round(duration_s, 3),
            "visual_cuts": len(visual_cuts),
            "scenes": len(scenes),
        }

    return {
        "project": str(project),
        "output": str(output_path),
        "source_videos": len(selected),
        "visual_cuts": total_visual_cuts,
        "scenes": total_scenes,
        "threshold": threshold,
        "min_scene_seconds": min_scene_seconds,
        "by_source": by_source,
    }


def detect_visual_cut_times(video_path: Path, *, threshold: float = DEFAULT_SCENE_THRESHOLD) -> list[float]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-i",
        str(video_path),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required. Install ffmpeg first.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"ffmpeg scene detection failed for {video_path}: {detail}") from exc

    return _parse_showinfo_cut_times(completed.stderr)


def build_scene_intervals(
    *,
    source_video_id: str,
    duration_s: float,
    visual_cut_times: list[float],
    non_content_ranges: list[dict[str, Any]] | None = None,
    min_scene_seconds: float = DEFAULT_MIN_SCENE_SECONDS,
) -> list[dict[str, Any]]:
    if duration_s <= 0:
        return []

    non_content = _normalize_non_content_ranges(non_content_ranges or [], duration_s=duration_s)
    boundaries: dict[float, set[str]] = defaultdict(set)
    _add_boundary(boundaries, 0.0, "source_start")
    _add_boundary(boundaries, duration_s, "source_end")
    for cut_time in visual_cut_times:
        cut = _number_or_none(cut_time)
        if cut is None or cut <= 0 or cut >= duration_s:
            continue
        _add_boundary(boundaries, cut, "visual_cut")
    for item in non_content:
        _add_boundary(boundaries, item["start_s"], "non_content_start")
        _add_boundary(boundaries, item["end_s"], "non_content_end")

    points = sorted(boundaries)
    segments = []
    for start_s, end_s in zip(points, points[1:], strict=False):
        duration = end_s - start_s
        if duration <= 0:
            continue
        non_content_item = _matching_non_content_range(start_s, end_s, non_content)
        if non_content_item:
            scene_type = "non_content"
            label = non_content_item["label"]
            relatedness = "non_content"
            confidence = non_content_item.get("confidence")
        else:
            scene_type = "content"
            label = "content"
            relatedness = ""
            confidence = None
        segments.append(
            {
                "source_video_id": source_video_id,
                "start_s": round(start_s, 3),
                "end_s": round(end_s, 3),
                "duration_s": round(duration, 3),
                "scene_type": scene_type,
                "label": label,
                "relatedness": relatedness,
                "confidence": confidence,
                "start_boundary_reasons": sorted(boundaries[start_s]),
                "end_boundary_reasons": sorted(boundaries[end_s]),
                "method": "ffmpeg_scene_select",
            }
        )

    return _merge_scene_segments(segments, min_scene_seconds=min_scene_seconds)


def _parse_showinfo_cut_times(stderr: str) -> list[float]:
    values = []
    seen = set()
    for line in stderr.splitlines():
        match = _PTS_TIME_RE.search(line)
        if not match:
            continue
        value = round(float(match.group(1)), 3)
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return sorted(values)


def _remove_scene_rows(path: Path, source_video_ids: set[str]) -> None:
    existing = read_jsonl(path)
    if not existing:
        return
    remaining = [row for row in existing if str(row.get("source_video_id") or "") not in source_video_ids]
    path.unlink()
    for row in remaining:
        append_jsonl(path, row)
    if not remaining:
        path.write_text("", encoding="utf-8")


def _non_content_by_source(project: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(project / "non_content_ranges.jsonl"):
        source_video_id = str(row.get("source_video_id") or "")
        if source_video_id:
            grouped[source_video_id].append(row)
    return dict(grouped)


def _normalize_non_content_ranges(rows: list[dict[str, Any]], *, duration_s: float) -> list[dict[str, Any]]:
    ranges = []
    for row in rows:
        start_s = _number_or_none(row.get("start_s"))
        end_s = _number_or_none(row.get("end_s"))
        if start_s is None or end_s is None:
            continue
        start = max(0.0, min(duration_s, start_s))
        end = max(0.0, min(duration_s, end_s))
        if end < start:
            start, end = end, start
        if end <= start:
            continue
        ranges.append(
            {
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "label": str(row.get("label") or "non_content"),
                "confidence": row.get("confidence"),
            }
        )
    return sorted(ranges, key=lambda item: (item["start_s"], item["end_s"]))


def _matching_non_content_range(
    start_s: float,
    end_s: float,
    ranges: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not ranges:
        return None
    midpoint = start_s + ((end_s - start_s) / 2.0)
    for item in ranges:
        if item["start_s"] <= midpoint <= item["end_s"]:
            return item
    return None


def _merge_scene_segments(segments: list[dict[str, Any]], *, min_scene_seconds: float) -> list[dict[str, Any]]:
    if min_scene_seconds <= 0:
        return [_finalize_scene(row) for row in segments]

    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(segments):
        segment = dict(segments[index])
        if (
            segment["scene_type"] == "content"
            and segment["duration_s"] < min_scene_seconds
            and merged
            and merged[-1]["scene_type"] == "content"
        ):
            _extend_segment(merged[-1], segment)
        elif (
            segment["scene_type"] == "content"
            and segment["duration_s"] < min_scene_seconds
            and index + 1 < len(segments)
            and segments[index + 1]["scene_type"] == "content"
        ):
            next_segment = dict(segments[index + 1])
            _prepend_segment(next_segment, segment)
            segments[index + 1] = next_segment
        else:
            merged.append(segment)
        index += 1

    coalesced: list[dict[str, Any]] = []
    for segment in merged:
        if (
            coalesced
            and coalesced[-1]["scene_type"] == "non_content"
            and segment["scene_type"] == "non_content"
            and coalesced[-1]["label"] == segment["label"]
        ):
            _extend_segment(coalesced[-1], segment)
        else:
            coalesced.append(segment)
    return [_finalize_scene(row) for row in coalesced]


def _extend_segment(left: dict[str, Any], right: dict[str, Any]) -> None:
    left["end_s"] = right["end_s"]
    left["duration_s"] = round(float(left["end_s"]) - float(left["start_s"]), 3)
    left["end_boundary_reasons"] = right.get("end_boundary_reasons") or []


def _prepend_segment(right: dict[str, Any], left: dict[str, Any]) -> None:
    right["start_s"] = left["start_s"]
    right["duration_s"] = round(float(right["end_s"]) - float(right["start_s"]), 3)
    right["start_boundary_reasons"] = left.get("start_boundary_reasons") or []


def _finalize_scene(row: dict[str, Any]) -> dict[str, Any]:
    scene = dict(row)
    if scene.get("confidence") is None:
        scene.pop("confidence", None)
    if not scene.get("relatedness"):
        scene.pop("relatedness", None)
    return scene


def _add_boundary(boundaries: dict[float, set[str]], value: float, reason: str) -> None:
    boundaries[round(float(value), 3)].add(reason)


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

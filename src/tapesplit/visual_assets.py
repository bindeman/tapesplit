from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


DEFAULT_KEYFRAME_WIDTH = 640
DEFAULT_THUMBNAIL_WIDTH = 240


def extract_visual_assets_for_project(
    project_dir: Path,
    *,
    subjects: list[str] | None = None,
    source_video_id: str | None = None,
    keyframe_width: int = DEFAULT_KEYFRAME_WIDTH,
    thumbnail_width: int = DEFAULT_THUMBNAIL_WIDTH,
    include_non_content: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if keyframe_width <= 0 or thumbnail_width <= 0:
        raise ValueError("keyframe and thumbnail widths must be greater than 0")
    selected_subjects = _normalize_subjects(subjects or ["scenes", "events"])
    tapes = read_jsonl(project / "tapes.jsonl")
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    tapes_by_id = {str(tape.get("id")): tape for tape in tapes if tape.get("id")}
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}
    visibility = build_visibility_filter(project)

    candidates: list[dict[str, Any]] = []
    if "scenes" in selected_subjects:
        candidates.extend(
            _scene_asset_candidates(
                project,
                source_video_id=source_video_id,
                include_non_content=include_non_content,
                visibility=visibility,
                evidence_by_id=evidence_by_id,
            )
        )
    if "events" in selected_subjects:
        candidates.extend(
            _event_asset_candidates(
                project,
                source_video_id=source_video_id,
                visibility=visibility,
                evidence_by_id=evidence_by_id,
            )
        )

    output_path = project / "visual_assets.jsonl"
    _remove_asset_rows(
        output_path,
        subject_types={_singular_subject(subject) for subject in selected_subjects},
        source_video_id=source_video_id,
    )
    next_asset_index = _next_index(output_path, prefix="visual_asset_")
    (project / "keyframes").mkdir(exist_ok=True)
    (project / "thumbnails").mkdir(exist_ok=True)

    written = 0
    skipped = 0
    for index, candidate in enumerate(candidates, start=next_asset_index):
        tape = tapes_by_id.get(candidate["source_video_id"])
        if not tape or not tape.get("path"):
            skipped += 1
            continue
        asset_id = f"visual_asset_{index:06d}"
        keyframe_rel = Path("keyframes") / f"{candidate['subject_type']}s" / f"{candidate['subject_id']}.jpg"
        thumbnail_rel = Path("thumbnails") / f"{candidate['subject_type']}s" / f"{candidate['subject_id']}.jpg"
        keyframe_path = project / keyframe_rel
        thumbnail_path = project / thumbnail_rel
        keyframe_path.parent.mkdir(parents=True, exist_ok=True)
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        if force or not keyframe_path.exists():
            _extract_frame(
                Path(tape["path"]),
                candidate["time_s"],
                keyframe_path,
                width=keyframe_width,
            )
        if force or not thumbnail_path.exists():
            _extract_frame(
                Path(tape["path"]),
                candidate["time_s"],
                thumbnail_path,
                width=thumbnail_width,
            )
        append_jsonl(
            output_path,
            {
                "id": asset_id,
                **candidate,
                "keyframe_path": str(keyframe_rel),
                "thumbnail_path": str(thumbnail_rel),
                "keyframe_width": keyframe_width,
                "thumbnail_width": thumbnail_width,
                "method": "ffmpeg_frame_extract",
                "review_status": "unreviewed",
            },
        )
        written += 1

    return {
        "project": str(project),
        "output": str(output_path),
        "subjects": selected_subjects,
        "source_video_id": source_video_id,
        "assets": written,
        "skipped": skipped,
        "keyframe_width": keyframe_width,
        "thumbnail_width": thumbnail_width,
    }


def _scene_asset_candidates(
    project: Path,
    *,
    source_video_id: str | None,
    include_non_content: bool,
    visibility: Any,
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = []
    for scene in read_jsonl(project / "scenes.jsonl"):
        if source_video_id and scene.get("source_video_id") != source_video_id:
            continue
        if not include_non_content and visibility.excluded_row(scene, evidence_by_id=evidence_by_id):
            continue
        if not include_non_content and scene.get("scene_type") == "non_content":
            continue
        source_id = str(scene.get("source_video_id") or "")
        if not source_id:
            continue
        start_s = _number_or_none(scene.get("start_s")) or 0.0
        end_s = _number_or_none(scene.get("end_s"))
        candidates.append(
            {
                "subject_type": "scene",
                "subject_id": str(scene.get("id") or ""),
                "source_video_id": source_id,
                "start_s": start_s,
                "end_s": end_s,
                "time_s": _representative_time(start_s, end_s),
                "label": str(scene.get("label") or scene.get("scene_type") or "scene"),
            }
        )
    return [candidate for candidate in candidates if candidate["subject_id"]]


def _event_asset_candidates(
    project: Path,
    *,
    source_video_id: str | None,
    visibility: Any,
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = []
    events = read_jsonl(project / "canonical_events.jsonl") or (
        read_jsonl(project / "events.jsonl") + read_jsonl(project / "gemini_events.jsonl")
    )
    for event in events:
        if visibility.excluded_row(event, evidence_by_id=evidence_by_id):
            continue
        source_range = _source_range_for_event(event, evidence_by_id)
        if not source_range:
            continue
        if source_video_id and source_range["source_video_id"] != source_video_id:
            continue
        candidates.append(
            {
                "subject_type": "event",
                "subject_id": str(event.get("id") or ""),
                "source_video_id": source_range["source_video_id"],
                "start_s": source_range["start_s"],
                "end_s": source_range.get("end_s"),
                "time_s": _representative_time(source_range["start_s"], source_range.get("end_s")),
                "label": str(event.get("title") or "event"),
            }
        )
    return [candidate for candidate in candidates if candidate["subject_id"]]


def _source_range_for_event(
    event: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    for source in [event.get("source_ranges"), metadata.get("source_ranges")]:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            source_video_id = str(item.get("source_video_id") or "")
            start_s = _number_or_none(item.get("start_s"))
            if source_video_id and start_s is not None:
                return {
                    "source_video_id": source_video_id,
                    "start_s": start_s,
                    "end_s": _number_or_none(item.get("end_s")),
                }
    source_video_id = str(event.get("source_video_id") or metadata.get("source_video_id") or "")
    start_s = _number_or_none(event.get("start_s"))
    if source_video_id and start_s is not None:
        return {
            "source_video_id": source_video_id,
            "start_s": start_s,
            "end_s": _number_or_none(event.get("end_s")),
        }
    for evidence_id in event.get("evidence_ids") or []:
        evidence = evidence_by_id.get(str(evidence_id))
        if not evidence:
            continue
        source_video_id = str(evidence.get("source_video_id") or "")
        start_s = _number_or_none(evidence.get("start_s"))
        if source_video_id and start_s is not None:
            return {
                "source_video_id": source_video_id,
                "start_s": start_s,
                "end_s": _number_or_none(evidence.get("end_s")),
            }
    return None


def _extract_frame(video_path: Path, time_s: float, output_path: Path, *, width: int) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, time_s):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "3",
        str(output_path),
        "-y",
    ]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required. Install ffmpeg first.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed to extract frame from {video_path}") from exc


def _remove_asset_rows(
    path: Path,
    *,
    subject_types: set[str],
    source_video_id: str | None,
) -> None:
    existing = read_jsonl(path)
    if not existing:
        return
    remaining = []
    for row in existing:
        if str(row.get("subject_type") or "") not in subject_types:
            remaining.append(row)
            continue
        if source_video_id and str(row.get("source_video_id") or "") != source_video_id:
            remaining.append(row)
            continue
    path.unlink()
    for row in remaining:
        append_jsonl(path, row)
    if not remaining:
        path.write_text("", encoding="utf-8")


def _next_index(path: Path, *, prefix: str) -> int:
    max_index = 0
    for row in read_jsonl(path):
        value = str(row.get("id") or "")
        if not value.startswith(prefix):
            continue
        try:
            max_index = max(max_index, int(value.removeprefix(prefix)))
        except ValueError:
            continue
    return max_index + 1


def _normalize_subjects(subjects: list[str]) -> list[str]:
    normalized = []
    for subject in subjects:
        for part in str(subject).split(","):
            value = part.strip().casefold()
            if not value:
                continue
            if value in {"scene", "scenes"}:
                value = "scenes"
            elif value in {"event", "events"}:
                value = "events"
            else:
                raise ValueError("subjects must contain scenes and/or events")
            if value not in normalized:
                normalized.append(value)
    return normalized


def _singular_subject(subject: str) -> str:
    return "scene" if subject == "scenes" else "event"


def _representative_time(start_s: float, end_s: float | None) -> float:
    if end_s is None or end_s <= start_s:
        return round(start_s, 3)
    # True midpoint: the first seconds of a range are its establishing shot
    # (or the tail of the previous recording); the middle shows the subject.
    return round(start_s + (end_s - start_s) / 2.0, 3)


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

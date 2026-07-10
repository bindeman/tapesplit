"""Second-opinion video analysis via Azure gpt-5.6 frame grids.

sol/terra take no video input and the platform caps requests at 50 images,
so a tape is analyzed as *scene-guided windows*: contiguous runs of content
scenes (≤45 scenes, ~8–12 min), one mid-scene frame each at 360p, every
frame labeled with its ABSOLUTE source second, plus the window's diarized
transcript. Timestamps ride inside the request, so the chunk-relative
offset failure class (task #13) is unrepresentable here by construction.

Output is written provider-tagged to ``azure_analyses.jsonl`` and is meant
for ADJUDICATION against the canonical event layer — never co-stitching
(``load_source_events(prefer_gemini=True)`` would drop it silently).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tapesplit.storage import append_jsonl, read_jsonl

ANALYSES_FILENAME = "azure_analyses.jsonl"
ADJUDICATION_FILENAME = "azure_adjudication.json"

MAX_IMAGES_PER_REQUEST = 45  # platform hard cap is 50; keep headroom
TARGET_WINDOW_SPAN_S = 720.0
FRAME_HEIGHT = 360
FRAME_JPEG_QUALITY = 7
TRANSCRIPT_MAX_CHARS = 6000

DEFAULT_SECOND_OPINION_DEPLOYMENT = "gpt-5.6-terra"

_TOKEN_RE = re.compile(r"[a-z0-9а-яё']+")


@dataclass(frozen=True)
class SceneWindow:
    source_video_id: str
    index: int
    start_s: float
    end_s: float
    scene_mids: tuple[float, ...]


@dataclass
class WindowResult:
    window: SceneWindow
    analysis: dict[str, Any]
    frames_used: int
    frames_dropped: int
    usage: dict[str, Any] = field(default_factory=dict)


def plan_scene_windows(
    project_dir: Path,
    source_video_id: str,
    *,
    max_scenes: int = MAX_IMAGES_PER_REQUEST,
    target_span_s: float = TARGET_WINDOW_SPAN_S,
) -> list[SceneWindow]:
    """Batch a tape's content scenes into frame-budget-sized windows."""

    project = project_dir.expanduser().resolve()
    scenes = [
        row
        for row in read_jsonl(project / "scenes.jsonl")
        if str(row.get("source_video_id") or "") == source_video_id
        and str(row.get("kind") or row.get("classification") or "content") != "non_content"
    ]
    scenes.sort(key=lambda r: float(r.get("start_s") or 0.0))

    windows: list[SceneWindow] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        mids = tuple(
            round((float(s.get("start_s") or 0.0) + float(s.get("end_s") or 0.0)) / 2.0, 3)
            for s in current
        )
        windows.append(
            SceneWindow(
                source_video_id=source_video_id,
                index=len(windows),
                start_s=float(current[0].get("start_s") or 0.0),
                end_s=float(current[-1].get("end_s") or 0.0),
                scene_mids=mids,
            )
        )
        current.clear()

    for scene in scenes:
        if current:
            span = float(scene.get("end_s") or 0.0) - float(current[0].get("start_s") or 0.0)
            if len(current) >= max_scenes or span > target_span_s:
                flush()
        current.append(scene)
    flush()
    return windows


def _extract_frame(source_path: Path, at_s: float, out_path: Path) -> bool:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, at_s):.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-vf",
        f"bwdif=mode=send_frame,scale=-2:{FRAME_HEIGHT}",
        "-q:v",
        str(FRAME_JPEG_QUALITY),
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode == 0 and out_path.exists()


def sample_window_frames(source_path: Path, window: SceneWindow) -> list[tuple[float, bytes]]:
    frames: list[tuple[float, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="tapesplit-azure-") as tmp:
        for i, mid in enumerate(window.scene_mids):
            out = Path(tmp) / f"f_{i:04d}.jpg"
            if _extract_frame(source_path, mid, out):
                frames.append((mid, out.read_bytes()))
    return frames


def _window_transcript(project: Path, window: SceneWindow) -> str:
    lines: list[str] = []
    for row in read_jsonl(project / "speaker_segments.jsonl"):
        if str(row.get("source_video_id") or "") != window.source_video_id:
            continue
        start = float(row.get("start_s") or 0.0)
        if start < window.start_s or start > window.end_s:
            continue
        text = str((row.get("metadata") or {}).get("transcript_text") or "").strip()
        if not text:
            continue
        who = str(row.get("speaker_label") or "Speaker")
        lines.append(f"[{start:.0f}s] {who}: {text}")
    joined = "\n".join(lines)
    return joined[:TRANSCRIPT_MAX_CHARS]


def _window_prompt(window: SceneWindow) -> str:
    return (
        "Analyze this segment of a digitized VHS/home-video transfer as an "
        "archival assistant. You are given one representative frame per scene "
        "— each labeled 'frame at <N>s:' where N is the ABSOLUTE second on the "
        "source tape — plus a diarized transcript with the same absolute "
        "timestamps. Return only valid JSON, numeric seconds everywhere. Every "
        f"start_s/end_s MUST lie within [{window.start_s:.0f}, {window.end_s:.0f}] "
        "and must be consistent with the frame labels; never invent timestamps "
        "outside this window.\n"
        "Hard limits: event_candidates max 6, person_mentions max 10, "
        "place_candidates max 6, date_candidates max 6, evidence_text max 3 "
        "snippets each under 80 characters.\n"
        "Goals: identify family/home-video content vs unrelated movie/TV/noise; "
        "build event candidates with timestamps, titles, grounded summaries, "
        "people, place clues, date clues, languages, confidence. Preserve "
        "uncertainty; do not invent identities, cities, or dates. Collapse "
        "repeated material into one range.\n"
        "Return this JSON shape: "
        '{"window_summary": str, "event_candidates": [{"title": str, '
        '"start_s": 0, "end_s": 0, "event_type": '
        '"birthday|school|holiday|travel|home|medical|conversation|unknown", '
        '"summary": str, "people": [str], "place_candidates": [str], '
        '"date_candidates": [str], "languages": [str], "relatedness": '
        '"likely_family|uncertain|likely_unrelated", "evidence_text": [str], '
        '"confidence": 0.0}], "person_mentions": [{"name": str, "start_s": 0, '
        '"end_s": 0, "evidence_text": str, "confidence": 0.0}], '
        '"place_candidates": [{"name": str, "start_s": 0, "end_s": 0, '
        '"evidence_text": str, "confidence": 0.0}], "date_candidates": '
        '[{"value": str, "start_s": 0, "end_s": 0, "evidence_text": str, '
        '"confidence": 0.0}], "uncertainties": [str]}'
    )


def _frame_parts(frames: list[tuple[float, bytes]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for at_s, jpeg in frames:
        parts.append({"type": "text", "text": f"frame at {at_s:.0f}s:"})
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                    "detail": "low",
                },
            }
        )
    return parts


def analyze_window(
    project: Path,
    source_path: Path,
    window: SceneWindow,
    *,
    deployment: str = DEFAULT_SECOND_OPINION_DEPLOYMENT,
    completion_fn: Callable[..., dict[str, Any]] | None = None,
) -> WindowResult:
    """Analyze one scene window; bisects frames on content-filter refusals."""

    from tapesplit.azure_openai_adapter import ContentPolicyViolation, reasoning_chat_completion

    complete = completion_fn or reasoning_chat_completion
    frames = sample_window_frames(source_path, window)
    transcript = _window_transcript(project, window)

    def request(batch: list[tuple[float, bytes]]) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": _window_prompt(window)}]
        content.extend(_frame_parts(batch))
        if transcript:
            content.append({"type": "text", "text": f"Diarized transcript:\n{transcript}"})
        if not batch:
            content.append(
                {"type": "text", "text": "No frames available; analyze from the transcript alone."}
            )
        return complete(
            deployment=deployment,
            messages=[{"role": "user", "content": content}],
            max_completion_tokens=3000,
            reasoning_effort="medium",
            response_format={"type": "json_object"},
            project_dir=project,
            operation="second_opinion_window",
        )

    dropped = 0
    try:
        result = request(frames)
    except ContentPolicyViolation:
        surviving: list[tuple[float, bytes]] = []
        stack = [frames]
        probes = 0
        while stack and probes < 8:
            batch = stack.pop()
            if not batch:
                continue
            probes += 1
            try:
                request(batch[:1] if len(batch) == 1 else batch)
                surviving.extend(batch)
            except ContentPolicyViolation:
                if len(batch) > 1:
                    mid = len(batch) // 2
                    stack.extend([batch[:mid], batch[mid:]])
        surviving.sort(key=lambda f: f[0])
        dropped = len(frames) - len(surviving)
        frames = surviving
        result = request(frames)

    message = (result.get("choices") or [{}])[0].get("message") or {}
    try:
        analysis = json.loads(message.get("content") or "{}")
    except (ValueError, TypeError):
        analysis = {}
    return WindowResult(
        window=window,
        analysis=analysis if isinstance(analysis, dict) else {},
        frames_used=len(frames),
        frames_dropped=dropped,
        usage=result.get("usage") or {},
    )


def analyze_video(
    project_dir: Path,
    source_video_id: str,
    *,
    deployment: str = DEFAULT_SECOND_OPINION_DEPLOYMENT,
    workers: int = 4,
    skip_existing: bool = True,
    completion_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Second-opinion analysis of one tape; appends to azure_analyses.jsonl."""

    from concurrent.futures import ThreadPoolExecutor, as_completed

    project = project_dir.expanduser().resolve()
    tapes = {str(r.get("id") or ""): r for r in read_jsonl(project / "tapes.jsonl")}
    tape = tapes.get(source_video_id)
    if tape is None:
        raise RuntimeError(f"unknown source video: {source_video_id}")
    source_path = Path(str(tape.get("path") or ""))
    if not source_path.exists():
        relative = tape.get("relative_path")
        if relative:
            source_path = project.parent / str(relative)
    if not source_path.exists():
        raise RuntimeError(f"source file missing for {source_video_id}")

    done_windows = {
        (str(r.get("source_video_id")), int((r.get("window") or {}).get("index", -1)))
        for r in read_jsonl(project / ANALYSES_FILENAME)
        if r.get("deployment") == deployment
    }
    windows = plan_scene_windows(project, source_video_id)
    pending = [
        w for w in windows if not (skip_existing and (source_video_id, w.index) in done_windows)
    ]

    lock = threading.Lock()
    written = 0
    failures: list[str] = []

    def work(window: SceneWindow) -> WindowResult:
        return analyze_window(
            project, source_path, window, deployment=deployment, completion_fn=completion_fn
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, w): w for w in pending}
        for future in as_completed(futures):
            window = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - per-window isolation
                failures.append(f"window {window.index}: {type(exc).__name__}: {exc}")
                continue
            with lock:
                append_jsonl(
                    project / ANALYSES_FILENAME,
                    {
                        "id": f"azure_analysis_{source_video_id}_{window.index:04d}",
                        "source_video_id": source_video_id,
                        "provider": "azure_openai",
                        "deployment": deployment,
                        "time_basis": "source_video",
                        "window": {
                            "index": window.index,
                            "start_s": window.start_s,
                            "end_s": window.end_s,
                            "scene_count": len(window.scene_mids),
                            "frames_used": result.frames_used,
                            "frames_dropped_by_filter": result.frames_dropped,
                        },
                        "analysis": result.analysis,
                        "usage": result.usage,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                written += 1

    return {
        "source_video_id": source_video_id,
        "windows_total": len(windows),
        "windows_analyzed": written,
        "windows_failed": failures,
        "deployment": deployment,
    }


# ---------------------------------------------------------------------------
# Adjudication against the canonical layer (never co-stitching)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(str(text).casefold()) if len(t) > 2}


def _overlap_s(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def adjudicate_against_canonical(project_dir: Path) -> dict[str, Any]:
    """Classify canonical events vs second-opinion events per covered range.

    Verdicts per canonical event within azure-covered windows:
    ``corroborated`` (range + content agree), ``range_disputed`` (content
    matches an azure event elsewhere on the tape), ``content_disputed``
    (overlapping azure event describes something else), ``uncovered``.
    Azure events with no canonical counterpart are reported as
    ``azure_only`` (possible missed events). Report is written to
    azure_adjudication.json; nothing is mutated.
    """

    project = project_dir.expanduser().resolve()
    azure_events: dict[str, list[dict[str, Any]]] = {}
    coverage: dict[str, list[tuple[float, float]]] = {}
    for row in read_jsonl(project / ANALYSES_FILENAME):
        video = str(row.get("source_video_id") or "")
        window = row.get("window") or {}
        coverage.setdefault(video, []).append(
            (float(window.get("start_s") or 0.0), float(window.get("end_s") or 0.0))
        )
        for candidate in (row.get("analysis") or {}).get("event_candidates") or []:
            try:
                start = float(candidate.get("start_s"))
                end = float(candidate.get("end_s"))
            except (TypeError, ValueError):
                continue
            azure_events.setdefault(video, []).append(
                {**candidate, "start_s": start, "end_s": end, "_matched": False}
            )

    media_offsets: dict[str, float] = {}
    try:
        viz = json.loads((project / "visualization.json").read_text())
        for media in viz.get("media", []):
            media_offsets[str(media.get("id") or "")] = float(media.get("offset_s") or 0.0)
    except (OSError, ValueError):
        pass

    def canonical_range(event: dict[str, Any]) -> tuple[str, float, float] | None:
        metadata = event.get("metadata") or {}
        ranges = metadata.get("source_ranges") or []
        best = None
        for r in ranges:
            video = str(r.get("source_video_id") or "")
            try:
                start, end = float(r.get("start_s")), float(r.get("end_s"))
            except (TypeError, ValueError):
                continue
            if best is None or (end - start) > (best[2] - best[1]):
                best = (video, start, end)
        if best is not None:
            return best
        video_ids = metadata.get("source_video_ids") or []
        video = str(video_ids[0]) if video_ids else ""
        if video and video in media_offsets and event.get("start_s") is not None:
            offset = media_offsets[video]
            return (video, float(event["start_s"]) - offset, float(event["end_s"]) - offset)
        return None

    verdicts: dict[str, list[dict[str, Any]]] = {
        "corroborated": [],
        "range_disputed": [],
        "content_disputed": [],
        "uncovered": [],
    }

    for event in read_jsonl(project / "canonical_events.jsonl"):
        resolved = canonical_range(event)
        if resolved is None:
            continue
        video, start, end = resolved
        covered = any(_overlap_s(start, end, w0, w1) > 0 for w0, w1 in coverage.get(video, []))
        entry = {
            "canonical_event_id": event.get("id"),
            "title": event.get("title"),
            "source_video_id": video,
            "start_s": start,
            "end_s": end,
        }
        if not covered:
            verdicts["uncovered"].append(entry)
            continue
        title_tokens = _tokens(event.get("title")) | _tokens(event.get("summary"))
        overlapping = [
            a
            for a in azure_events.get(video, [])
            if _overlap_s(start, end, a["start_s"], a["end_s"])
            > 0.3 * max(1.0, min(end - start, a["end_s"] - a["start_s"]))
        ]
        best_overlap = None
        for a in overlapping:
            score = len(title_tokens & (_tokens(a.get("title")) | _tokens(a.get("summary"))))
            if best_overlap is None or score > best_overlap[0]:
                best_overlap = (score, a)
        if best_overlap and best_overlap[0] >= 2:
            best_overlap[1]["_matched"] = True
            verdicts["corroborated"].append(
                {**entry, "azure_title": best_overlap[1].get("title")}
            )
            continue
        elsewhere = None
        for a in azure_events.get(video, []):
            score = len(title_tokens & (_tokens(a.get("title")) | _tokens(a.get("summary"))))
            if score >= 3 and _overlap_s(start, end, a["start_s"], a["end_s"]) == 0:
                elsewhere = a
                break
        if elsewhere is not None:
            elsewhere["_matched"] = True
            verdicts["range_disputed"].append(
                {
                    **entry,
                    "azure_title": elsewhere.get("title"),
                    "azure_range": [elsewhere["start_s"], elsewhere["end_s"]],
                }
            )
        elif overlapping:
            verdicts["content_disputed"].append(
                {**entry, "azure_title": overlapping[0].get("title")}
            )
        else:
            verdicts["uncovered"].append(entry)

    azure_only = [
        {
            "source_video_id": video,
            "title": a.get("title"),
            "start_s": a["start_s"],
            "end_s": a["end_s"],
            "confidence": a.get("confidence"),
            "relatedness": a.get("relatedness"),
        }
        for video, events in azure_events.items()
        for a in events
        if not a["_matched"] and str(a.get("relatedness") or "") == "likely_family"
    ]

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "counts": {key: len(rows) for key, rows in verdicts.items()}
        | {"azure_only": len(azure_only)},
        "verdicts": verdicts,
        "azure_only": azure_only,
    }
    (project / ADJUDICATION_FILENAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report

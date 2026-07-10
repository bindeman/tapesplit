"""Shot-bounded face tracks: track people through scenes, not lucky keyframes.

The keyframe pipeline samples one frame per scene, so a person's only
observation in a scene is a coin flip on transient blur/pose — measured on the
family haul this left 6,346 content scenes entirely unsampled for faces and
fragmented one boy across 100+ clusters. This stage decodes each face-bearing
scene at a few fps (deinterlaced), detects+embeds every face per frame in one
InsightFace pass, associates detections within the shot (IoU + embedding gate;
scene cuts bound tracks so no cross-cut drift is possible), and emits one
quality-weighted aggregate embedding per track. Clustering then works on
tracks: profile/blurred/occluded frames inherit identity from the clean
frontal frames in the same track, and concurrent tracks become cannot-link
constraints (two people on screen together are never the same person).

Track ids are content-hashed (media/scene/start/first-bbox), never positional,
so downstream references survive re-runs. Completed scenes checkpoint to a
partial file so a killed run resumes instead of recomputing hours of decode.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import math
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import Any

from tapesplit.claim_store import DualWriter
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visual_assets import FRAME_EXTRACT_FILTERS

FACE_TRACKS_FILENAME = "face_tracks.jsonl"
FACE_TRACKS_PARTIAL_FILENAME = "face_tracks.partial.jsonl"
FACE_TRACKS_VECTORS_FILENAME = "face_tracks.npz"
TRACK_THUMBNAIL_DIR = Path("thumbnails") / "tracks"

DEFAULT_TRACK_FPS = 5.0
DEFAULT_TRACK_MIN_SIZE = 36
DEFAULT_TRACK_MIN_FRAMES = 2
# A track ends when its person is unmatched for this long (occlusion/pan slack).
TRACK_MAX_GAP_S = 1.2
# Association gates: strong spatial overlap matches outright; weak overlap
# needs the embedding to agree (prevents identity switches when faces cross).
TRACK_IOU_MATCH = 0.25
TRACK_IOU_WEAK = 0.05
TRACK_WEAK_MAX_COSINE_DISTANCE = 0.55
# Aggregate embeddings average the best frames; beyond this many the tail is
# blur and profiles that only add noise to the template.
TRACK_TOP_K_FRAMES = 4
# Joining existing keyframe observations to a track: nearest frame sample must
# be close in time and overlap the (rescaled) observation bbox.
MEMBER_JOIN_TIME_TOLERANCE_S = 0.75
MEMBER_JOIN_MIN_IOU = 0.3

# Frame-quality weight mirrors face_quality's ramp: identity signal fades
# below ArcFace's 112px alignment size and vanishes near 24px.
QUALITY_MIN_SIDE_FLOOR_PX = 24
QUALITY_MIN_SIDE_FULL_PX = 112
QUALITY_DET_SCORE_FULL = 0.72


def build_face_tracks_for_project(
    project_dir: Path,
    *,
    fps: float = DEFAULT_TRACK_FPS,
    source_video_id: str | None = None,
    min_size: int = DEFAULT_TRACK_MIN_SIZE,
    min_frames: int = DEFAULT_TRACK_MIN_FRAMES,
    scene_scope: str = "face-bearing",
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if fps <= 0:
        raise ValueError("fps must be greater than 0")
    if scene_scope not in {"face-bearing", "content"}:
        raise ValueError("scene_scope must be face-bearing or content")

    tapes = {str(tape.get("id")): tape for tape in read_jsonl(project / "tapes.jsonl") if tape.get("id")}
    if not tapes:
        raise FileNotFoundError(f"missing project tapes file: {project / 'tapes.jsonl'}")
    observations = read_jsonl(project / "face_observations.jsonl")
    scenes = _target_scenes(
        project, observations, scene_scope=scene_scope, source_video_id=source_video_id
    )
    if not scenes:
        return {
            "project": str(project),
            "scenes_targeted": 0,
            "tracks": 0,
            "note": "no scenes in scope (run detect-faces first for face-bearing scoping)",
        }

    detector = _InsightFaceFrameAnalyzer()
    partial_path = project / FACE_TRACKS_PARTIAL_FILENAME
    done_scene_ids: set[str] = set()
    partial_rows: list[dict[str, Any]] = []
    if force and partial_path.exists():
        partial_path.unlink()
    if not force:
        # A killed run may truncate its final line. Track rows are written
        # BEFORE the scene_done marker, so only marker-confirmed scenes count
        # as done; anything after the last marker simply recomputes.
        recovered = _read_partial_rows(partial_path)
        for row in recovered:
            if row.get("kind") == "scene_done" and row.get("scene_id"):
                done_scene_ids.add(str(row["scene_id"]))
        for row in recovered:
            if row.get("kind") == "track" and str(row.get("scene_id") or "") in done_scene_ids:
                partial_rows.append(row)

    (project / TRACK_THUMBNAIL_DIR).mkdir(parents=True, exist_ok=True)
    obs_by_scene = _observations_by_scene(observations)

    frames_processed = 0
    scenes_processed = 0
    scenes_skipped_done = 0
    new_rows: list[dict[str, Any]] = []
    for scene in scenes:
        scene_id = str(scene.get("id") or "")
        if scene_id in done_scene_ids:
            scenes_skipped_done += 1
            continue
        tape = tapes.get(str(scene.get("source_video_id") or ""))
        if not tape or not tape.get("path") or not Path(str(tape["path"])).exists():
            continue
        tracks, frame_count = _track_scene(
            project,
            scene,
            tape,
            detector=detector,
            fps=fps,
            min_size=min_size,
            min_frames=min_frames,
        )
        frames_processed += frame_count
        for track in tracks:
            track["members"] = _join_member_observations(
                track, obs_by_scene.get(scene_id, []), tape=tape
            )
        rows = [_track_row(track) for track in tracks]
        # Rows first, marker last: the scene_done line commits the scene, so a
        # kill mid-checkpoint can never mark a scene done with missing tracks.
        for row in rows:
            append_jsonl(partial_path, {"kind": "track", "scene_id": scene_id, **row})
        append_jsonl(partial_path, {"kind": "scene_done", "scene_id": scene_id})
        new_rows.extend(rows)
        scenes_processed += 1
        if scenes_processed % 20 == 0:
            print(
                f"[face-tracks] {scenes_processed + scenes_skipped_done}/{len(scenes)} scenes "
                f"({len(partial_rows) + len(new_rows)} tracks, {frames_processed} frames)",
                flush=True,
            )

    all_rows = _dedupe_tracks(partial_rows + new_rows)
    for row in all_rows:
        row.pop("kind", None)
    _consolidate(project, all_rows)
    if partial_path.exists():
        partial_path.unlink()

    claims = _write_presence_claims(project, all_rows, tapes, detector.feature_model)
    return {
        "project": str(project),
        "scene_scope": scene_scope,
        "fps": fps,
        "scenes_targeted": len(scenes),
        "scenes_processed": scenes_processed,
        "scenes_resumed": scenes_skipped_done,
        "frames_processed": frames_processed,
        "tracks": len(all_rows),
        "tracks_with_members": sum(1 for row in all_rows if row.get("member_face_observation_ids")),
        "member_observations_joined": sum(
            len(row.get("member_face_observation_ids") or []) for row in all_rows
        ),
        "feature_model": detector.feature_model,
        "claims": claims,
        "output": str(project / FACE_TRACKS_FILENAME),
    }


def load_face_tracks(project: Path) -> list[dict[str, Any]]:
    return [row for row in read_jsonl(project / FACE_TRACKS_FILENAME) if isinstance(row, dict)]


def track_vectors_for_model(
    project: Path, tracks: list[dict[str, Any]], embedder: Any
) -> dict[str, list[float]]:
    """Track vectors in `embedder`'s space, re-embedding saved crops on mismatch.

    Tracks aggregate in the space of the model that built them; when clustering
    resolves a different backend (e.g. KP-RPE lands after a buffalo_l run), the
    per-track best-frame crops are re-embedded and the vectors persisted, so
    all clustering units always share one embedding space.
    """

    np = _load_numpy()
    vectors_path = project / FACE_TRACKS_VECTORS_FILENAME
    stored: dict[str, Any] = {}
    if vectors_path.exists():
        try:
            with np.load(vectors_path) as handle:
                stored = {key: handle[key].copy() for key in handle.files}
        except Exception:
            stored = {}

    target_model = str(embedder.feature_model)
    result: dict[str, list[float]] = {}
    changed = False
    for track in tracks:
        track_id = str(track.get("id") or "")
        if not track_id:
            continue
        if str(track.get("feature_model") or "") == target_model and track_id in stored:
            result[track_id] = [float(value) for value in stored[track_id]]
            continue
        vectors = []
        weights = []
        for sample in track.get("top_frames") or []:
            crop_rel = str(sample.get("crop_path") or "")
            if not crop_rel:
                continue
            described = None
            try:
                described = embedder.describe(project, {"face_thumbnail_path": crop_rel})
            except RuntimeError:
                raise
            except Exception:
                described = None
            if described and described.get("vector"):
                vectors.append(described["vector"])
                weights.append(float(sample.get("quality_w") or 0.1))
        aggregated = _weighted_mean_normalized(vectors, weights)
        if aggregated is None:
            continue
        stored[track_id] = np.asarray(aggregated, dtype=np.float32)
        result[track_id] = aggregated
        track["feature_model"] = target_model
        changed = True

    if changed:
        if stored:
            np.savez_compressed(vectors_path, **stored)
        _consolidate(project, tracks)
    return result


def write_track_cluster_assignments(
    project: Path, tracks: list[dict[str, Any]], assignments: dict[str, str]
) -> None:
    for track in tracks:
        track_id = str(track.get("id") or "")
        if track_id in assignments:
            track["face_cluster_id"] = assignments[track_id]
    _consolidate(project, tracks)


# --- per-scene tracking -----------------------------------------------------


def _track_scene(
    project: Path,
    scene: dict[str, Any],
    tape: dict[str, Any],
    *,
    detector: "_InsightFaceFrameAnalyzer",
    fps: float,
    min_size: int,
    min_frames: int,
) -> tuple[list[dict[str, Any]], int]:
    cv2 = _load_cv2()
    start_s = float(scene.get("start_s") or 0.0)
    end_s = float(scene.get("end_s") or 0.0)
    duration = max(0.0, end_s - start_s)
    if duration <= 0:
        return [], 0

    frame_count = 0
    open_tracks: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="tapesplit_tracks_") as tmp:
        tmp_dir = Path(tmp)
        _decode_scene_frames(Path(str(tape["path"])), start_s, duration, fps, tmp_dir)
        for index, frame_path in enumerate(sorted(tmp_dir.glob("*.jpg"))):
            frame_time = start_s + (index + 0.5) / fps
            image = cv2.imread(str(frame_path))
            if image is None:
                continue
            frame_count += 1
            detections = detector.analyze(image, min_size=min_size)
            _associate(
                open_tracks,
                closed,
                detections,
                frame_time=frame_time,
                image=image,
            )
            _expire_tracks(open_tracks, closed, now_s=frame_time)
    closed.extend(open_tracks)

    tracks = []
    for state in closed:
        if len(state["samples"]) < min_frames:
            continue
        track = _finalize_track(project, state, scene=scene)
        if track:
            tracks.append(track)
    _mark_co_occurrence(tracks)
    return tracks, frame_count


def _decode_scene_frames(video_path: Path, start_s: float, duration: float, fps: float, out_dir: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_s):.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration:.3f}",
        "-vf",
        f"{FRAME_EXTRACT_FILTERS},fps={fps}",
        "-q:v",
        "4",
        str(out_dir / "%05d.jpg"),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"ffmpeg failed to decode scene frames from {video_path}") from exc


def _associate(
    open_tracks: list[dict[str, Any]],
    closed: list[dict[str, Any]],
    detections: list[dict[str, Any]],
    *,
    frame_time: float,
    image: Any,
) -> None:
    candidates = []
    for detection_index, detection in enumerate(detections):
        for track in open_tracks:
            iou = _bbox_iou(detection["bbox"], track["samples"][-1]["bbox"])
            if iou < TRACK_IOU_WEAK:
                continue
            distance = _cosine_distance(detection.get("vector"), track["last_vector"])
            if iou < TRACK_IOU_MATCH and distance > TRACK_WEAK_MAX_COSINE_DISTANCE:
                continue
            candidates.append((-iou, distance, detection_index, id(track), track))
    candidates.sort(key=lambda item: (item[0], item[1]))

    matched_detections: set[int] = set()
    matched_tracks: set[int] = set()
    for negative_iou, _distance, detection_index, track_key, track in candidates:
        if detection_index in matched_detections or track_key in matched_tracks:
            continue
        matched_detections.add(detection_index)
        matched_tracks.add(track_key)
        _append_sample(track, detections[detection_index], frame_time=frame_time, image=image)

    for detection_index, detection in enumerate(detections):
        if detection_index in matched_detections:
            continue
        track = {"samples": [], "last_vector": None, "best": None, "top": []}
        _append_sample(track, detection, frame_time=frame_time, image=image)
        open_tracks.append(track)


def _append_sample(track: dict[str, Any], detection: dict[str, Any], *, frame_time: float, image: Any) -> None:
    bbox = detection["bbox"]
    quality_w = _frame_quality_weight(bbox, detection.get("det_score"))
    sample = {
        "t": round(frame_time, 3),
        "bbox": bbox,
        "det_score": _round_or_none(detection.get("det_score")),
        "quality_w": round(quality_w, 4),
        "age_raw": _round_or_none(detection.get("age_raw")),
        "gender_raw": detection.get("gender_raw"),
    }
    track["samples"].append(sample)
    if detection.get("vector") is not None:
        track["last_vector"] = detection["vector"]
        track.setdefault("vectors", []).append((quality_w, detection["vector"]))
    crop = _crop_bbox(image, bbox)
    entry = {"quality_w": quality_w, "sample": sample, "crop": crop}
    top = track["top"]
    top.append(entry)
    top.sort(key=lambda item: -item["quality_w"])
    del top[TRACK_TOP_K_FRAMES:]
    if track["best"] is None or quality_w > track["best"]["quality_w"]:
        track["best"] = entry


def _expire_tracks(open_tracks: list[dict[str, Any]], closed: list[dict[str, Any]], *, now_s: float) -> None:
    still_open = []
    for track in open_tracks:
        if now_s - track["samples"][-1]["t"] > TRACK_MAX_GAP_S:
            closed.append(track)
        else:
            still_open.append(track)
    open_tracks[:] = still_open


def _finalize_track(
    project: Path, state: dict[str, Any], *, scene: dict[str, Any]
) -> dict[str, Any] | None:
    samples = state["samples"]
    vectors = state.get("vectors") or []
    aggregated = _weighted_mean_normalized(
        [vector for _weight, vector in vectors], [weight for weight, _vector in vectors]
    )
    if aggregated is None:
        return None
    media_id = str(scene.get("source_video_id") or "")
    scene_id = str(scene.get("id") or "")
    first = samples[0]
    track_id = "track_" + hashlib.sha1(
        f"{media_id}:{scene_id}:{first['t']:.1f}:"
        f"{first['bbox']['x']}-{first['bbox']['y']}-{first['bbox']['width']}-{first['bbox']['height']}".encode()
    ).hexdigest()[:12]

    cv2 = _load_cv2()
    (project / TRACK_THUMBNAIL_DIR).mkdir(parents=True, exist_ok=True)
    top_frames = []
    for rank, entry in enumerate(state["top"], start=1):
        crop_rel = TRACK_THUMBNAIL_DIR / f"{track_id}_k{rank}.jpg"
        if entry["crop"] is not None:
            cv2.imwrite(str(project / crop_rel), entry["crop"])
        top_frames.append(
            {
                "t": entry["sample"]["t"],
                "bbox": entry["sample"]["bbox"],
                "quality_w": entry["sample"]["quality_w"],
                "crop_path": str(crop_rel),
            }
        )
    representative_rel = TRACK_THUMBNAIL_DIR / f"{track_id}.jpg"
    if state["best"] is not None and state["best"]["crop"] is not None:
        cv2.imwrite(str(project / representative_rel), state["best"]["crop"])

    weights = [float(sample["quality_w"]) for sample in samples]
    ages = [float(sample["age_raw"]) for sample in samples if sample.get("age_raw") is not None]
    age_weights = [
        float(sample["quality_w"]) for sample in samples if sample.get("age_raw") is not None
    ]
    det_scores = [float(sample["det_score"]) for sample in samples if sample.get("det_score") is not None]
    return {
        "id": track_id,
        "media_id": media_id,
        "scene_id": scene_id,
        "span": {"clock": "source", "start_s": samples[0]["t"], "end_s": samples[-1]["t"]},
        "frame_count": len(samples),
        "frame_samples": samples,
        "top_frames": top_frames,
        "representative": {
            "t": state["best"]["sample"]["t"] if state["best"] else samples[0]["t"],
            "bbox": state["best"]["sample"]["bbox"] if state["best"] else samples[0]["bbox"],
            "thumbnail_path": str(representative_rel),
        },
        "quality": {
            "mean_w": round(sum(weights) / len(weights), 4),
            "max_w": round(max(weights), 4),
            "n_effective": round(sum(weights), 2),
        },
        "age_estimate": round(_weighted_median(ages, age_weights), 1) if ages else None,
        "gender_raw": _majority(
            [str(sample.get("gender_raw") or "") for sample in samples if sample.get("gender_raw")]
        ),
        "mean_det_score": round(sum(det_scores) / len(det_scores), 4) if det_scores else None,
        "member_face_observation_ids": [],
        "co_occurring_track_ids": [],
        "face_cluster_id": "",
        "feature_model": "",  # stamped by the aggregate-vector write below
        "_vector": aggregated,
    }


def _mark_co_occurrence(tracks: list[dict[str, Any]]) -> None:
    for left_index in range(len(tracks)):
        for right_index in range(left_index + 1, len(tracks)):
            left, right = tracks[left_index], tracks[right_index]
            if _spans_overlap(left["span"], right["span"]):
                left["co_occurring_track_ids"].append(right["id"])
                right["co_occurring_track_ids"].append(left["id"])


def _spans_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return float(a["start_s"]) <= float(b["end_s"]) and float(b["start_s"]) <= float(a["end_s"])


def _join_member_observations(
    track: dict[str, Any], observations: list[dict[str, Any]], *, tape: dict[str, Any]
) -> list[str]:
    """Existing keyframe observations join the track by time + rescaled-bbox IoU."""

    native_width = _number_or_none(((tape.get("probe") or {}).get("width"))) or 0.0
    members = []
    for observation in observations:
        obs_time = _number_or_none(observation.get("time_s") or observation.get("start_s"))
        if obs_time is None:
            continue
        span = track["span"]
        if not (
            span["start_s"] - MEMBER_JOIN_TIME_TOLERANCE_S
            <= obs_time
            <= span["end_s"] + MEMBER_JOIN_TIME_TOLERANCE_S
        ):
            continue
        nearest = min(track["frame_samples"], key=lambda sample: abs(sample["t"] - obs_time))
        if abs(nearest["t"] - obs_time) > MEMBER_JOIN_TIME_TOLERANCE_S:
            continue
        obs_bbox = _rescale_bbox(
            observation.get("bbox") or {},
            keyframe_width=_number_or_none(observation.get("keyframe_width")) or 640.0,
            native_width=native_width,
        )
        if _bbox_iou(obs_bbox, nearest["bbox"]) >= MEMBER_JOIN_MIN_IOU:
            members.append(str(observation.get("id") or ""))
    return sorted({member for member in members if member})


def _rescale_bbox(bbox: dict[str, Any], *, keyframe_width: float, native_width: float) -> dict[str, Any]:
    if not bbox or native_width <= 0 or keyframe_width <= 0:
        return bbox
    scale = native_width / keyframe_width
    return {
        "x": float(bbox.get("x") or 0) * scale,
        "y": float(bbox.get("y") or 0) * scale,
        "width": float(bbox.get("width") or 0) * scale,
        "height": float(bbox.get("height") or 0) * scale,
    }


# --- persistence + claims ---------------------------------------------------


def _track_row(track: dict[str, Any]) -> dict[str, Any]:
    row = {key: value for key, value in track.items() if not key.startswith("_")}
    row["member_face_observation_ids"] = track.get("members") or row.get(
        "member_face_observation_ids", []
    )
    row.pop("members", None)
    return row


def _read_partial_rows(path: Path) -> list[dict[str, Any]]:
    """Checkpoint reader that survives a truncated final line from a killed run."""

    import json

    if not path.exists():
        return []
    rows = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _dedupe_tracks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        track_id = str(row.get("id") or "")
        if track_id:
            by_id[track_id] = row
    return sorted(
        by_id.values(),
        key=lambda row: (str(row.get("media_id") or ""), float(row["span"]["start_s"])),
    )


def _consolidate(project: Path, rows: list[dict[str, Any]]) -> None:
    np = _load_numpy()
    vectors_path = project / FACE_TRACKS_VECTORS_FILENAME
    stored: dict[str, Any] = {}
    if vectors_path.exists():
        try:
            with np.load(vectors_path) as handle:
                stored = {key: handle[key].copy() for key in handle.files}
        except Exception:
            stored = {}
    default_model = _InsightFaceFrameAnalyzer.feature_model
    for row in rows:
        vector = row.pop("_vector", None)
        if vector is not None:
            stored[str(row["id"])] = np.asarray(vector, dtype=np.float32)
            row["feature_model"] = row.get("feature_model") or default_model
    if stored:
        np.savez_compressed(vectors_path, **stored)

    path = project / FACE_TRACKS_FILENAME
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)
    if not rows:
        path.write_text("", encoding="utf-8")


def load_track_vectors(project: Path) -> dict[str, list[float]]:
    np = _load_numpy()
    vectors_path = project / FACE_TRACKS_VECTORS_FILENAME
    if not vectors_path.exists():
        return {}
    try:
        with np.load(vectors_path) as handle:
            return {key: [float(value) for value in handle[key]] for key in handle.files}
    except Exception:
        return {}


def _write_presence_claims(
    project: Path,
    rows: list[dict[str, Any]],
    tapes: dict[str, dict[str, Any]],
    feature_model: str,
) -> dict[str, Any]:
    writer = DualWriter.open(
        project,
        artifact=FACE_TRACKS_FILENAME,
        producer=f"face-tracker/insightface+{feature_model}",
    )
    writer.supersede_previous()
    for row in rows:
        tape = tapes.get(str(row.get("media_id") or "")) or {}
        duration = _number_or_none((tape.get("probe") or {}).get("duration_s"))
        writer.write_row(
            row,
            kind="presence",
            media_id=str(row.get("media_id") or "") or None,
            start_s=float(row["span"]["start_s"]),
            end_s=float(row["span"]["end_s"]),
            confidence=_number_or_none(row.get("mean_det_score")),
            media_duration_s=duration,
            assertion={
                "track_id": row.get("id"),
                "scene_id": row.get("scene_id"),
                "frame_count": row.get("frame_count"),
                "age_estimate": row.get("age_estimate"),
            },
        )
    return {"claims_written": writer.claims_written, "error": writer.error}


# --- detection --------------------------------------------------------------


class _InsightFaceFrameAnalyzer:
    """Full-frame SCRFD detection + ArcFace embedding + attributes, one pass."""

    feature_model = "insightface_buffalo_l_arcface_512"

    def __init__(self) -> None:
        self._app: Any | None = None

    def analyze(self, image: Any, *, min_size: int) -> list[dict[str, Any]]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            observations = self._face_app().get(image)
        detections = []
        for observation in observations or []:
            bbox_values = getattr(observation, "bbox", None)
            if bbox_values is None or len(bbox_values) < 4:
                continue
            x1, y1, x2, y2 = (float(value) for value in bbox_values[:4])
            bbox = {
                "x": max(0.0, x1),
                "y": max(0.0, y1),
                "width": max(0.0, x2 - x1),
                "height": max(0.0, y2 - y1),
            }
            if min(bbox["width"], bbox["height"]) < min_size:
                continue
            embedding = getattr(observation, "normed_embedding", None)
            if embedding is None:
                embedding = getattr(observation, "embedding", None)
            detections.append(
                {
                    "bbox": bbox,
                    "vector": [float(value) for value in embedding] if embedding is not None else None,
                    "det_score": float(getattr(observation, "det_score", 0.0) or 0.0),
                    "age_raw": getattr(observation, "age", None),
                    "gender_raw": str(getattr(observation, "sex", "") or ""),
                }
            )
        return sorted(detections, key=lambda item: (item["bbox"]["x"], item["bbox"]["y"]))

    def _face_app(self) -> Any:
        if self._app is not None:
            return self._app
        try:
            from insightface.app import FaceAnalysis  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "InsightFace is required for face tracking. Install with "
                "`python -m pip install -e '.[face-ai]'`."
            ) from exc
        from tapesplit.face_clustering import _onnxruntime_providers

        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore", FutureWarning)
            app = FaceAnalysis(name="buffalo_l", providers=_onnxruntime_providers())
            app.prepare(ctx_id=-1, det_size=(640, 640))
        self._app = app
        return app


def face_tracks_available() -> bool:
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


# --- scene selection --------------------------------------------------------


def _target_scenes(
    project: Path,
    observations: list[dict[str, Any]],
    *,
    scene_scope: str,
    source_video_id: str | None,
) -> list[dict[str, Any]]:
    face_scene_ids = {
        str(observation.get("source_subject_id") or "")
        for observation in observations
        if str(observation.get("source_subject_type") or "") == "scene"
    }
    scenes = []
    for scene in read_jsonl(project / "scenes.jsonl"):
        if str(scene.get("scene_type") or "") == "non_content":
            continue
        if source_video_id and str(scene.get("source_video_id") or "") != source_video_id:
            continue
        if scene_scope == "face-bearing" and str(scene.get("id") or "") not in face_scene_ids:
            continue
        scenes.append(scene)
    scenes.sort(key=lambda scene: (str(scene.get("source_video_id") or ""), float(scene.get("start_s") or 0)))
    return scenes


def _observations_by_scene(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        if str(observation.get("source_subject_type") or "") != "scene":
            continue
        grouped.setdefault(str(observation.get("source_subject_id") or ""), []).append(observation)
    return grouped


# --- math helpers -----------------------------------------------------------


def _frame_quality_weight(bbox: dict[str, Any], det_score: Any) -> float:
    min_side = min(float(bbox.get("width") or 0), float(bbox.get("height") or 0))
    size_factor = max(
        0.0,
        min(
            1.0,
            (min_side - QUALITY_MIN_SIDE_FLOOR_PX)
            / float(QUALITY_MIN_SIDE_FULL_PX - QUALITY_MIN_SIDE_FLOOR_PX),
        ),
    ) ** 0.5
    score = _number_or_none(det_score)
    det_factor = 1.0 if score is None else max(0.2, min(1.0, score / QUALITY_DET_SCORE_FULL))
    return max(0.01, size_factor * det_factor)


def _bbox_iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax1, ay1 = float(a.get("x") or 0), float(a.get("y") or 0)
    ax2, ay2 = ax1 + float(a.get("width") or 0), ay1 + float(a.get("height") or 0)
    bx1, by1 = float(b.get("x") or 0), float(b.get("y") or 0)
    bx2, by2 = bx1 + float(b.get("width") or 0), by1 + float(b.get("height") or 0)
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = inter_w * inter_h
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def _cosine_distance(left: Any, right: Any) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left)) or 1.0
    norm_right = math.sqrt(sum(b * b for b in right)) or 1.0
    return max(0.0, min(2.0, 1.0 - dot / (norm_left * norm_right)))


def _weighted_mean_normalized(vectors: list[Any], weights: list[float]) -> list[float] | None:
    if not vectors:
        return None
    dims = len(vectors[0])
    total = sum(weights) or 1.0
    values = [
        sum(float(vector[index]) * weight for vector, weight in zip(vectors, weights, strict=False))
        / total
        for index in range(dims)
    ]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _weighted_median(values: list[float], weights: list[float]) -> float:
    if not values:
        return 0.0
    if not weights or len(weights) != len(values):
        weights = [1.0] * len(values)
    pairs = sorted(zip(values, weights, strict=False))
    total = sum(weight for _value, weight in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= total / 2:
            return value
    return pairs[-1][0]


def _majority(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get)


def _crop_bbox(image: Any, bbox: dict[str, Any], *, padding: float = 0.25) -> Any:
    height, width = image.shape[:2]
    x = int(float(bbox.get("x") or 0))
    y = int(float(bbox.get("y") or 0))
    box_w = int(float(bbox.get("width") or 0))
    box_h = int(float(bbox.get("height") or 0))
    pad_x, pad_y = int(box_w * padding), int(box_h * padding)
    left, top = max(0, x - pad_x), max(0, y - pad_y)
    right, bottom = min(width, x + box_w + pad_x), min(height, y + box_h + pad_y)
    if right <= left or bottom <= top:
        return None
    return image[top:bottom, left:right].copy()


def _round_or_none(value: Any) -> float | None:
    number = _number_or_none(value)
    return round(number, 4) if number is not None else None


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_numpy() -> Any:
    try:
        import numpy  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for face tracks. Install with `python -m pip install -e '.[vision]'`."
        ) from exc
    return numpy


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for face tracks. Install with `python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2


__all__ = [
    "build_face_tracks_for_project",
    "face_tracks_available",
    "load_face_tracks",
    "load_track_vectors",
    "track_vectors_for_model",
    "write_track_cluster_assignments",
    "FACE_TRACKS_FILENAME",
]

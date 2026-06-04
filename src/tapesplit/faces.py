from __future__ import annotations

from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_FACE_MIN_SIZE = 40


def check_face_detection_config() -> dict[str, Any]:
    try:
        cv2 = _load_cv2()
    except RuntimeError:
        return {"opencv": False, "opencv_version": ""}
    return {"opencv": True, "opencv_version": str(getattr(cv2, "__version__", ""))}


def detect_face_thumbnails_for_project(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    subject_type: str = "scene",
    min_size: int = DEFAULT_FACE_MIN_SIZE,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if min_size <= 0:
        raise ValueError("min_size must be greater than 0")
    subject_type = _normalize_subject_type(subject_type)
    assets = [
        asset
        for asset in read_jsonl(project / "visual_assets.jsonl")
        if asset.get("subject_type") == subject_type
        and (source_video_id is None or asset.get("source_video_id") == source_video_id)
    ]
    if not assets:
        raise FileNotFoundError("no matching visual assets found; run `tapesplit extract-visuals` first")

    output_path = project / "face_observations.jsonl"
    _remove_face_rows(output_path, source_video_id=source_video_id, subject_type=subject_type)
    next_face_index = _next_index(output_path, prefix="face_observation_")
    (project / "thumbnails" / "faces").mkdir(parents=True, exist_ok=True)

    face_count = 0
    scanned_assets = 0
    for asset in assets:
        image_path = project / str(asset.get("keyframe_path") or asset.get("thumbnail_path") or "")
        if not image_path.exists():
            continue
        scanned_assets += 1
        detections = _detect_faces_in_image(image_path, min_size=min_size)
        for detection_index, detection in enumerate(detections, start=1):
            face_count += 1
            face_id = f"face_observation_{next_face_index + face_count - 1:06d}"
            face_rel = Path("thumbnails") / "faces" / f"{asset['subject_id']}_{detection_index:03d}.jpg"
            face_path = project / face_rel
            if force or not face_path.exists():
                _crop_face_thumbnail(image_path, face_path, detection["bbox"])
            append_jsonl(
                output_path,
                {
                    "id": face_id,
                    "source_video_id": asset.get("source_video_id"),
                    "source_subject_type": asset.get("subject_type"),
                    "source_subject_id": asset.get("subject_id"),
                    "start_s": asset.get("start_s"),
                    "end_s": asset.get("end_s"),
                    "time_s": asset.get("time_s"),
                    "bbox": detection["bbox"],
                    "bbox_format": "pixel_xywh",
                    "face_thumbnail_path": str(face_rel),
                    "detector": detection.get("detector") or "opencv_haar_frontalface_default",
                    "confidence": detection.get("confidence"),
                    "person_group_id": "",
                    "review_status": "unreviewed",
                },
            )

    return {
        "project": str(project),
        "output": str(output_path),
        "subject_type": subject_type,
        "source_video_id": source_video_id,
        "visual_assets_scanned": scanned_assets,
        "faces": face_count,
        "min_size": min_size,
    }


def _detect_faces_in_image(image_path: Path, *, min_size: int) -> list[dict[str, Any]]:
    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    detector = cv2.CascadeClassifier(cascade_path)
    faces = detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(min_size, min_size),
    )
    detections = []
    for x, y, width, height in faces:
        detections.append(
            {
                "bbox": {
                    "x": int(x),
                    "y": int(y),
                    "width": int(width),
                    "height": int(height),
                },
                "detector": "opencv_haar_frontalface_default",
            }
        )
    return detections


def _crop_face_thumbnail(image_path: Path, output_path: Path, bbox: dict[str, Any], *, padding: float = 0.25) -> None:
    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        return
    height, width = image.shape[:2]
    x, y, box_width, box_height = _expanded_bbox(bbox, image_width=width, image_height=height, padding=padding)
    crop = image[y : y + box_height, x : x + box_width]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), crop)


def _expanded_bbox(
    bbox: dict[str, Any],
    *,
    image_width: int,
    image_height: int,
    padding: float,
) -> tuple[int, int, int, int]:
    x = int(float(bbox.get("x") or 0))
    y = int(float(bbox.get("y") or 0))
    width = int(float(bbox.get("width") or 0))
    height = int(float(bbox.get("height") or 0))
    pad_x = int(width * padding)
    pad_y = int(height * padding)
    left = max(0, x - pad_x)
    top = max(0, y - pad_y)
    right = min(image_width, x + width + pad_x)
    bottom = min(image_height, y + height + pad_y)
    return left, top, max(0, right - left), max(0, bottom - top)


def _remove_face_rows(path: Path, *, source_video_id: str | None, subject_type: str) -> None:
    existing = read_jsonl(path)
    if not existing:
        return
    remaining = []
    for row in existing:
        if str(row.get("source_subject_type") or "") != subject_type:
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


def _normalize_subject_type(value: str) -> str:
    subject_type = value.strip().casefold()
    if subject_type in {"scene", "scenes"}:
        return "scene"
    if subject_type in {"event", "events"}:
        return "event"
    raise ValueError("subject_type must be scene or event")


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for local face thumbnails. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2

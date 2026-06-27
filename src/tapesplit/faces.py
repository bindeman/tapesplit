from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from tapesplit.face_quality import analyze_face_quality
from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_FACE_MIN_SIZE = 40
DEFAULT_FACE_DETECTION_BACKEND = "auto"
FACE_DETECTION_BACKENDS = {"auto", "opencv", "apple-vision"}


def check_face_detection_config() -> dict[str, Any]:
    try:
        cv2 = _load_cv2()
    except RuntimeError:
        opencv = False
        opencv_version = ""
    else:
        opencv = True
        opencv_version = str(getattr(cv2, "__version__", ""))

    apple_vision = _apple_vision_available()
    return {
        "opencv": opencv,
        "opencv_version": opencv_version,
        "apple_vision": apple_vision,
        "apple_vision_platform": platform.system() == "Darwin",
        "face_detection_default_backend": _resolve_face_detection_backend("auto", require_available=False),
    }


def detect_face_thumbnails_for_project(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    subject_type: str = "scene",
    min_size: int = DEFAULT_FACE_MIN_SIZE,
    backend: str = DEFAULT_FACE_DETECTION_BACKEND,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if min_size <= 0:
        raise ValueError("min_size must be greater than 0")
    subject_type = _normalize_subject_type(subject_type)
    resolved_backend = _resolve_face_detection_backend(backend)
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
        detections = _detect_faces_in_image(image_path, min_size=min_size, backend=resolved_backend)
        for detection_index, detection in enumerate(detections, start=1):
            face_count += 1
            face_id = f"face_observation_{next_face_index + face_count - 1:06d}"
            face_rel = Path("thumbnails") / "faces" / f"{asset['subject_id']}_{detection_index:03d}.jpg"
            face_path = project / face_rel
            if force or not face_path.exists():
                _crop_face_thumbnail(image_path, face_path, detection["bbox"])
            quality = analyze_face_quality(face_path)
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
                    "face_quality": quality,
                    "face_quality_status": quality.get("status"),
                    "face_quality_notes": quality.get("notes") or [],
                    "detector": detection.get("detector") or "opencv_haar_frontalface_default",
                    "detection_backend": resolved_backend,
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
        "requested_backend": _normalize_face_detection_backend(backend),
        "resolved_backend": resolved_backend,
    }


def _detect_faces_in_image(
    image_path: Path,
    *,
    min_size: int,
    backend: str = DEFAULT_FACE_DETECTION_BACKEND,
) -> list[dict[str, Any]]:
    resolved_backend = _resolve_face_detection_backend(backend)
    if resolved_backend == "apple-vision":
        return _detect_faces_with_apple_vision(image_path, min_size=min_size)
    if resolved_backend == "opencv":
        return _detect_faces_with_opencv(image_path, min_size=min_size)
    raise ValueError(f"unsupported face detection backend: {backend}")


def _detect_faces_with_opencv(image_path: Path, *, min_size: int) -> list[dict[str, Any]]:
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


def _detect_faces_with_apple_vision(image_path: Path, *, min_size: int) -> list[dict[str, Any]]:
    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        return []
    image_height, image_width = image.shape[:2]

    Vision, NSURL = _load_apple_vision()
    url = NSURL.fileURLWithPath_(str(image_path))
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    result = handler.performRequests_error_([request], None)
    success = result[0] if isinstance(result, tuple) else bool(result)
    error = result[1] if isinstance(result, tuple) and len(result) > 1 else None
    if not success:
        raise RuntimeError(f"Apple Vision face detection failed for {image_path}: {error}")

    detections = []
    for observation in request.results() or []:
        bbox = _vision_rect_to_pixel_bbox(
            observation.boundingBox(),
            image_width=image_width,
            image_height=image_height,
        )
        if bbox["width"] < min_size or bbox["height"] < min_size:
            continue
        detections.append(
            {
                "bbox": bbox,
                "detector": "apple_vision_face_rectangles",
                "confidence": round(float(observation.confidence()), 4),
            }
        )
    return sorted(detections, key=lambda item: (item["bbox"]["x"], item["bbox"]["y"]))


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


def _normalize_face_detection_backend(value: str) -> str:
    backend = value.strip().casefold().replace("_", "-")
    aliases = {
        "apple": "apple-vision",
        "vision": "apple-vision",
        "coreml": "apple-vision",
        "cv2": "opencv",
        "opencv-haar": "opencv",
    }
    backend = aliases.get(backend, backend)
    if backend not in FACE_DETECTION_BACKENDS:
        raise ValueError("face detection backend must be auto, opencv, or apple-vision")
    return backend


def _resolve_face_detection_backend(value: str, *, require_available: bool = True) -> str:
    backend = _normalize_face_detection_backend(value)
    if backend == "auto":
        if _apple_vision_available():
            return "apple-vision"
        return "opencv"
    if backend == "apple-vision":
        if platform.system() != "Darwin":
            raise RuntimeError("Apple Vision face detection is only available on macOS")
        if not _apple_vision_available():
            if require_available:
                raise RuntimeError(
                    "Apple Vision face detection requires PyObjC. Install with "
                    "`python -m pip install -e '.[macos]'`."
                )
            return "opencv"
    return backend


def _apple_vision_available() -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        _load_apple_vision()
    except RuntimeError:
        return False
    return True


def _vision_rect_to_pixel_bbox(
    rect: Any,
    *,
    image_width: int,
    image_height: int,
) -> dict[str, int]:
    normalized_x, normalized_y, normalized_width, normalized_height = _vision_rect_components(rect)
    x = int(round(normalized_x * image_width))
    y = int(round((1.0 - normalized_y - normalized_height) * image_height))
    width = int(round(normalized_width * image_width))
    height = int(round(normalized_height * image_height))

    left = max(0, min(image_width, x))
    top = max(0, min(image_height, y))
    right = max(left, min(image_width, x + width))
    bottom = max(top, min(image_height, y + height))
    return {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }


def _vision_rect_components(rect: Any) -> tuple[float, float, float, float]:
    if hasattr(rect, "origin") and hasattr(rect, "size"):
        origin = _maybe_call(getattr(rect, "origin"))
        size = _maybe_call(getattr(rect, "size"))
        return (
            float(_point_value(origin, "x", 0)),
            float(_point_value(origin, "y", 1)),
            float(_point_value(size, "width", 0)),
            float(_point_value(size, "height", 1)),
        )
    if isinstance(rect, (tuple, list)) and len(rect) == 2:
        origin, size = rect
        return (
            float(_point_value(origin, "x", 0)),
            float(_point_value(origin, "y", 1)),
            float(_point_value(size, "width", 0)),
            float(_point_value(size, "height", 1)),
        )
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        return (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    raise TypeError(f"unsupported Apple Vision rect type: {type(rect)!r}")


def _point_value(value: Any, attr: str, index: int) -> Any:
    if hasattr(value, attr):
        return _maybe_call(getattr(value, attr))
    return value[index]


def _maybe_call(value: Any) -> Any:
    return value() if callable(value) else value


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for local face thumbnails. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2


def _load_apple_vision() -> tuple[Any, Any]:
    try:
        import Vision  # type: ignore
        from Foundation import NSURL  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyObjC Vision bindings are required for Apple Vision face detection. "
            "Install with `python -m pip install -e '.[macos]'`."
        ) from exc
    return Vision, NSURL

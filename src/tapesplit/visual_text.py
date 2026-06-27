from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_TEXT_RECOGNITION_BACKEND = "auto"
TEXT_RECOGNITION_BACKENDS = {"auto", "apple-vision"}
DEFAULT_TEXT_MIN_CONFIDENCE = 0.3


def check_visual_text_config() -> dict[str, Any]:
    return {
        "apple_vision_ocr": _apple_vision_text_available(),
        "visual_text_default_backend": _resolve_text_recognition_backend("auto", require_available=False),
    }


def detect_text_for_project(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    subject_type: str | None = None,
    backend: str = DEFAULT_TEXT_RECOGNITION_BACKEND,
    min_confidence: float = DEFAULT_TEXT_MIN_CONFIDENCE,
    languages: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if min_confidence < 0 or min_confidence > 1:
        raise ValueError("min_confidence must be between 0 and 1")
    resolved_backend = _resolve_text_recognition_backend(backend)
    normalized_subject_type = _normalize_subject_type(subject_type)
    assets = [
        asset
        for asset in read_jsonl(project / "visual_assets.jsonl")
        if (normalized_subject_type is None or asset.get("subject_type") == normalized_subject_type)
        and (source_video_id is None or asset.get("source_video_id") == source_video_id)
    ]
    if not assets:
        raise FileNotFoundError("no matching visual assets found; run `tapesplit extract-visuals` first")

    output_path = project / "visual_text_observations.jsonl"
    _remove_text_rows(output_path, source_video_id=source_video_id, subject_type=normalized_subject_type)
    next_index = _next_index(output_path, prefix="visual_text_")

    scanned_assets = 0
    observation_count = 0
    for asset in assets:
        image_path = project / str(asset.get("keyframe_path") or asset.get("thumbnail_path") or "")
        if not image_path.exists():
            continue
        scanned_assets += 1
        detections = _detect_text_in_image(
            image_path,
            backend=resolved_backend,
            min_confidence=min_confidence,
            languages=languages,
        )
        for detection in detections:
            observation_count += 1
            observation_id = f"visual_text_{next_index + observation_count - 1:06d}"
            append_jsonl(
                output_path,
                {
                    "id": observation_id,
                    "visual_asset_id": asset.get("id"),
                    "source_video_id": asset.get("source_video_id"),
                    "source_subject_type": asset.get("subject_type"),
                    "source_subject_id": asset.get("subject_id"),
                    "start_s": asset.get("start_s"),
                    "end_s": asset.get("end_s"),
                    "time_s": asset.get("time_s"),
                    "text": detection["text"],
                    "confidence": detection.get("confidence"),
                    "bbox": detection.get("bbox"),
                    "bbox_format": "pixel_xywh" if detection.get("bbox") else "",
                    "text_backend": resolved_backend,
                    "engine": detection.get("engine"),
                    "recognition_languages": languages or [],
                    "source_image_path": str(asset.get("keyframe_path") or asset.get("thumbnail_path") or ""),
                    "review_status": "unreviewed",
                },
            )

    return {
        "project": str(project),
        "output": str(output_path),
        "requested_backend": _normalize_text_recognition_backend(backend),
        "resolved_backend": resolved_backend,
        "source_video_id": source_video_id,
        "subject_type": normalized_subject_type or "all",
        "visual_assets_scanned": scanned_assets,
        "text_observations": observation_count,
        "min_confidence": min_confidence,
        "languages": languages or [],
    }


def _detect_text_in_image(
    image_path: Path,
    *,
    backend: str,
    min_confidence: float,
    languages: list[str] | None,
) -> list[dict[str, Any]]:
    resolved_backend = _resolve_text_recognition_backend(backend)
    if resolved_backend == "apple-vision":
        return _detect_text_with_apple_vision(image_path, min_confidence=min_confidence, languages=languages)
    raise ValueError(f"unsupported text recognition backend: {backend}")


def _detect_text_with_apple_vision(
    image_path: Path,
    *,
    min_confidence: float,
    languages: list[str] | None,
) -> list[dict[str, Any]]:
    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        return []
    image_height, image_width = image.shape[:2]

    Vision, NSURL = _load_apple_vision()
    request = Vision.VNRecognizeTextRequest.alloc().init()
    if hasattr(Vision, "VNRequestTextRecognitionLevelAccurate"):
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    if hasattr(request, "setUsesLanguageCorrection_"):
        request.setUsesLanguageCorrection_(True)
    if hasattr(request, "setAutomaticallyDetectsLanguage_"):
        request.setAutomaticallyDetectsLanguage_(True)
    if languages:
        request.setRecognitionLanguages_(languages)

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image_path)), {})
    result = handler.performRequests_error_([request], None)
    success = result[0] if isinstance(result, tuple) else bool(result)
    error = result[1] if isinstance(result, tuple) and len(result) > 1 else None
    if not success:
        raise RuntimeError(f"Apple Vision text recognition failed for {image_path}: {error}")

    detections = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string() or "").strip()
        confidence = float(candidate.confidence())
        if not text or confidence < min_confidence:
            continue
        detections.append(
            {
                "text": text,
                "confidence": round(confidence, 4),
                "bbox": _vision_rect_to_pixel_bbox(
                    observation.boundingBox(),
                    image_width=image_width,
                    image_height=image_height,
                ),
                "engine": "apple_vision_recognize_text",
            }
        )
    return sorted(detections, key=lambda item: (item["bbox"]["y"], item["bbox"]["x"], item["text"]))


def _remove_text_rows(path: Path, *, source_video_id: str | None, subject_type: str | None) -> None:
    existing = read_jsonl(path)
    if not existing:
        return
    remaining = []
    for row in existing:
        if subject_type and str(row.get("source_subject_type") or "") != subject_type:
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


def _normalize_subject_type(value: str | None) -> str | None:
    if value is None:
        return None
    subject_type = value.strip().casefold()
    if subject_type in {"all", "*", ""}:
        return None
    if subject_type in {"scene", "scenes"}:
        return "scene"
    if subject_type in {"event", "events"}:
        return "event"
    raise ValueError("subject_type must be all, scene, or event")


def _normalize_text_recognition_backend(value: str) -> str:
    backend = value.strip().casefold().replace("_", "-")
    aliases = {"apple": "apple-vision", "vision": "apple-vision", "coreml": "apple-vision"}
    backend = aliases.get(backend, backend)
    if backend not in TEXT_RECOGNITION_BACKENDS:
        raise ValueError("text recognition backend must be auto or apple-vision")
    return backend


def _resolve_text_recognition_backend(value: str, *, require_available: bool = True) -> str:
    backend = _normalize_text_recognition_backend(value)
    if backend == "auto":
        if _apple_vision_text_available():
            return "apple-vision"
        if require_available:
            raise RuntimeError(
                "No local OCR backend is available. On macOS install with "
                "`python -m pip install -e '.[macos]'`."
            )
        return "unavailable"
    if backend == "apple-vision":
        if platform.system() != "Darwin":
            raise RuntimeError("Apple Vision text recognition is only available on macOS")
        if not _apple_vision_text_available():
            if require_available:
                raise RuntimeError(
                    "Apple Vision text recognition requires PyObjC. Install with "
                    "`python -m pip install -e '.[macos]'`."
                )
            return "unavailable"
    return backend


def _apple_vision_text_available() -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        Vision, _NSURL = _load_apple_vision()
    except RuntimeError:
        return False
    return hasattr(Vision, "VNRecognizeTextRequest")


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
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


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
            "OpenCV is required for local OCR image dimension checks. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2


def _load_apple_vision() -> tuple[Any, Any]:
    try:
        import Vision  # type: ignore
        from Foundation import NSURL  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyObjC Vision bindings are required for Apple Vision text recognition. "
            "Install with `python -m pip install -e '.[macos]'`."
        ) from exc
    return Vision, NSURL

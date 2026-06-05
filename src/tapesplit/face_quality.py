from __future__ import annotations

from pathlib import Path
from typing import Any


def analyze_face_quality(image_path: Path) -> dict[str, Any]:
    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        return {
            "usable": False,
            "status": "missing_image",
            "notes": ["Face thumbnail could not be read."],
        }
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    eye_count = _eye_count(cv2, gray, min_side=min(width, height))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    quality = {
        "width": int(width),
        "height": int(height),
        "min_side": int(min(width, height)),
        "eye_count": int(eye_count),
        "sharpness": round(sharpness, 3),
        "brightness": round(brightness, 3),
    }
    status, notes = _quality_status(quality)
    return {
        **quality,
        "usable": status == "usable",
        "status": status,
        "notes": notes,
    }


def _eye_count(cv2: Any, gray: Any, *, min_side: int) -> int:
    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_eye.xml")
    detector = cv2.CascadeClassifier(cascade_path)
    min_eye = max(6, min_side // 8)
    eyes = detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=3,
        minSize=(min_eye, min_eye),
    )
    return int(len(eyes))


def _quality_status(quality: dict[str, Any]) -> tuple[str, list[str]]:
    notes = []
    min_side = int(quality.get("min_side") or 0)
    eye_count = int(quality.get("eye_count") or 0)
    sharpness = float(quality.get("sharpness") or 0.0)
    brightness = float(quality.get("brightness") or 0.0)

    if min_side < 64:
        notes.append("Face crop is too small for reliable identity clustering.")
    if min_side < 96 and eye_count == 0:
        notes.append("Small crop has no detected eyes; likely unreliable or a false positive.")
    if sharpness < 8.0:
        notes.append("Face crop is very blurry.")
    if brightness < 18.0 or brightness > 238.0:
        notes.append("Face crop has extreme brightness.")

    return ("low_quality", notes) if notes else ("usable", [])


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for local face quality checks. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2

from __future__ import annotations

from pathlib import Path
from typing import Any

# Continuous quality weighting replaced the old binary usable/low-quality gate.
# The Haar eye-cascade check is gone on purpose: measured on the family haul it
# false-gated 496 real faces (sunglasses, closed eyes, profiles, soft VHS
# focus) and manufactured 508 singleton "review-only" clusters — 71% of all
# clusters. Quality now informs WEIGHTS (aggregation, seeding, confidence),
# never exclusion; clustering decides what to do with weak crops.

# min_side ramp: ArcFace-class models align at 112x112; below ~24px a crop
# carries almost no identity signal.
QUALITY_MIN_SIDE_FLOOR_PX = 24
QUALITY_MIN_SIDE_FULL_PX = 112
# Laplacian variance saturates fast on soft VHS footage; anything >= 60 is
# "sharp for this medium". Sharpness only modulates half the weight so a soft
# but large frontal crop still scores well.
QUALITY_SHARPNESS_FULL = 60.0
QUALITY_BRIGHTNESS_LOW = 30.0
QUALITY_BRIGHTNESS_HIGH = 225.0
# Status vocabulary is kept for the UI ("usable" / "low_quality"), but it is
# display-only downstream of this module.
QUALITY_LOW_STATUS_WEIGHT = 0.3


def analyze_face_quality(image_path: Path) -> dict[str, Any]:
    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        return {
            "usable": False,
            "status": "missing_image",
            "quality_weight": 0.0,
            "notes": ["Face thumbnail could not be read."],
        }
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    quality = {
        "width": int(width),
        "height": int(height),
        "min_side": int(min(width, height)),
        "sharpness": round(sharpness, 3),
        "brightness": round(brightness, 3),
    }
    weight = _quality_weight(quality)
    status, notes = _quality_status(quality, weight)
    return {
        **quality,
        "quality_weight": weight,
        "usable": True,
        "status": status,
        "notes": notes,
    }


def _quality_weight(quality: dict[str, Any]) -> float:
    min_side = float(quality.get("min_side") or 0.0)
    sharpness = float(quality.get("sharpness") or 0.0)
    brightness = float(quality.get("brightness") or 0.0)

    size_factor = _clamp(
        (min_side - QUALITY_MIN_SIDE_FLOOR_PX)
        / float(QUALITY_MIN_SIDE_FULL_PX - QUALITY_MIN_SIDE_FLOOR_PX)
    )
    size_factor = size_factor**0.5
    sharpness_factor = _clamp(sharpness / QUALITY_SHARPNESS_FULL)
    if brightness < QUALITY_BRIGHTNESS_LOW:
        brightness_factor = max(0.2, brightness / QUALITY_BRIGHTNESS_LOW)
    elif brightness > QUALITY_BRIGHTNESS_HIGH:
        brightness_factor = max(0.2, (255.0 - brightness) / (255.0 - QUALITY_BRIGHTNESS_HIGH))
    else:
        brightness_factor = 1.0

    weight = size_factor * (0.5 + 0.5 * sharpness_factor) * brightness_factor
    return round(_clamp(weight), 4)


def _quality_status(quality: dict[str, Any], weight: float) -> tuple[str, list[str]]:
    notes = []
    min_side = int(quality.get("min_side") or 0)
    sharpness = float(quality.get("sharpness") or 0.0)
    brightness = float(quality.get("brightness") or 0.0)

    if min_side < 64:
        notes.append("Face crop is small; identity signal is weak.")
    if sharpness < 8.0:
        notes.append("Face crop is very blurry.")
    if brightness < 18.0 or brightness > 238.0:
        notes.append("Face crop has extreme brightness.")

    status = "usable" if weight >= QUALITY_LOW_STATUS_WEIGHT else "low_quality"
    return status, notes


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for local face quality checks. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2

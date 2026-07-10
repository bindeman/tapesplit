from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from tapesplit.storage import append_jsonl, read_jsonl


FACE_EMBEDDINGS_INDEX = "face_embeddings.jsonl"
FACE_EMBEDDINGS_VECTORS = "face_embeddings.npz"


def ensure_face_embeddings(
    project: Path,
    faces: list[dict[str, Any]],
    *,
    feature_model: str,
    describe: Callable[[dict[str, Any]], dict[str, Any] | None],
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return one embedding record per face observation, computing only what's missing.

    Embeddings are persisted to face_embeddings.npz (vectors) plus
    face_embeddings.jsonl (index rows keyed by observation id, carrying the
    span/bbox anchor, the feature model, a content hash of the crop, and the
    attributes captured in the same pass: det_score, age_raw, gender_raw,
    pose). A record is reused when the observation id, feature model, and crop
    content hash all match; anything else is recomputed via `describe`.
    """

    np = _load_numpy()
    index_path = project / FACE_EMBEDDINGS_INDEX
    vectors_path = project / FACE_EMBEDDINGS_VECTORS

    existing_rows = {} if force else {str(row.get("id") or ""): row for row in read_jsonl(index_path)}
    existing_vectors: dict[str, Any] = {}
    if not force and vectors_path.exists():
        try:
            with np.load(vectors_path) as handle:
                existing_vectors = {key: handle[key].copy() for key in handle.files}
        except Exception:
            existing_vectors = {}

    records: dict[str, dict[str, Any]] = {}
    vectors: dict[str, Any] = {}
    computed = 0
    reused = 0
    failed = 0
    for face in faces:
        face_id = str(face.get("id") or "")
        if not face_id:
            continue
        content_hash = _crop_content_hash(project, face)
        cached = existing_rows.get(face_id)
        if (
            cached
            and str(cached.get("model") or "") == feature_model
            and str(cached.get("content_hash") or "") == content_hash
            and face_id in existing_vectors
        ):
            record = dict(cached)
            record["vector"] = [float(value) for value in existing_vectors[face_id]]
            records[face_id] = record
            vectors[face_id] = existing_vectors[face_id]
            reused += 1
            continue

        described = None
        try:
            described = describe(face)
        except RuntimeError:
            raise
        except Exception:
            described = None
        record = _index_row(face, feature_model=feature_model, content_hash=content_hash)
        if described and described.get("vector"):
            vector = np.asarray(described["vector"], dtype=np.float32)
            record.update(
                {
                    "embedded": True,
                    "dimensions": int(vector.shape[0]),
                    "det_score": _round_or_none(described.get("det_score")),
                    "age_raw": _round_or_none(described.get("age_raw")),
                    "gender_raw": described.get("gender_raw"),
                    "pose": described.get("pose") or {},
                }
            )
            record["vector"] = [float(value) for value in vector]
            vectors[face_id] = vector
            computed += 1
        else:
            record["embedded"] = False
            failed += 1
        records[face_id] = record

    _write_index(index_path, records)
    if vectors:
        np.savez_compressed(vectors_path, **vectors)
    elif vectors_path.exists():
        vectors_path.unlink()
    summary = {"computed": computed, "reused": reused, "failed": failed}
    for record in records.values():
        record.setdefault("vector", None)
    return {"records": records, "summary": summary}


def load_face_embeddings(project: Path) -> dict[str, dict[str, Any]]:
    """Read persisted embedding records (with vectors) without computing anything."""

    np = _load_numpy()
    index_path = project / FACE_EMBEDDINGS_INDEX
    vectors_path = project / FACE_EMBEDDINGS_VECTORS
    rows = {str(row.get("id") or ""): dict(row) for row in read_jsonl(index_path)}
    if vectors_path.exists():
        try:
            with np.load(vectors_path) as handle:
                for key in handle.files:
                    if key in rows:
                        rows[key]["vector"] = [float(value) for value in handle[key]]
        except Exception:
            pass
    for row in rows.values():
        row.setdefault("vector", None)
    return rows


def face_anchor(face: dict[str, Any]) -> dict[str, Any]:
    """The durable address of a face: media + source-clock span + bbox.

    Anchors survive observation-id renumbering (re-detection) and cluster-id
    renumbering (re-clustering); every human face decision carries them.
    """

    time_s = _number_or_none(face.get("time_s"))
    start_s = _number_or_none(face.get("start_s"))
    end_s = _number_or_none(face.get("end_s"))
    anchor_start = time_s if time_s is not None else start_s
    anchor_end = time_s if time_s is not None else end_s
    return {
        "media_id": str(face.get("source_video_id") or ""),
        "span": {
            "clock": "source",
            "start_s": anchor_start,
            "end_s": anchor_end if anchor_end is not None else anchor_start,
        },
        "bbox": dict(face.get("bbox") or {}),
        "face_observation_id": str(face.get("id") or ""),
    }


def anchor_key(face: dict[str, Any]) -> tuple[str, float, int, int] | None:
    """Coarse anchor key for set overlap across runs (succession matching).

    Time is rounded to 0.1s and the bbox center is bucketed to 16px so the key
    is stable across re-detection jitter but still separates two faces in the
    same frame.
    """

    media_id = str(face.get("source_video_id") or "")
    time_s = _number_or_none(face.get("time_s"))
    if time_s is None:
        time_s = _number_or_none(face.get("start_s"))
    bbox = face.get("bbox") or {}
    if not media_id or time_s is None or not bbox:
        return None
    center_x = float(bbox.get("x") or 0) + float(bbox.get("width") or 0) / 2.0
    center_y = float(bbox.get("y") or 0) + float(bbox.get("height") or 0) / 2.0
    return (media_id, round(time_s, 1), int(center_x // 16), int(center_y // 16))


def _index_row(face: dict[str, Any], *, feature_model: str, content_hash: str) -> dict[str, Any]:
    anchor = face_anchor(face)
    return {
        "id": str(face.get("id") or ""),
        "media_id": anchor["media_id"],
        "span": anchor["span"],
        "bbox": anchor["bbox"],
        "model": feature_model,
        "content_hash": content_hash,
    }


def _crop_content_hash(project: Path, face: dict[str, Any]) -> str:
    path = project / str(face.get("face_thumbnail_path") or "")
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _write_index(path: Path, records: dict[str, dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    rows_written = 0
    for face_id in sorted(records):
        row = {key: value for key, value in records[face_id].items() if key != "vector"}
        append_jsonl(path, row)
        rows_written += 1
    if not rows_written:
        path.write_text("", encoding="utf-8")


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
            "NumPy is required for persisted face embeddings. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return numpy

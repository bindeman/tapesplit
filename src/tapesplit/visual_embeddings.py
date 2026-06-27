from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_VISUAL_EMBEDDING_BACKEND = "auto"
DEFAULT_VISUAL_EMBEDDING_MODEL = "sentence-transformers/clip-ViT-B-32"
VISUAL_EMBEDDING_BACKENDS = {"auto", "sentence-transformers"}


def check_visual_embedding_config() -> dict[str, Any]:
    resolved = _resolve_visual_embedding_backend("auto", require_available=False)
    return {
        "visual_embedding_sentence_transformers": _sentence_transformers_available(),
        "visual_embedding_default_backend": resolved,
        "visual_embedding_default_model": DEFAULT_VISUAL_EMBEDDING_MODEL if resolved == "sentence-transformers" else "",
    }


def embed_visual_assets_for_project(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    subject_type: str | None = None,
    backend: str = DEFAULT_VISUAL_EMBEDDING_BACKEND,
    model_name: str = DEFAULT_VISUAL_EMBEDDING_MODEL,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    resolved_backend = _resolve_visual_embedding_backend(backend)
    normalized_subject_type = _normalize_subject_type(subject_type)
    assets = [
        asset
        for asset in read_jsonl(project / "visual_assets.jsonl")
        if (normalized_subject_type is None or asset.get("subject_type") == normalized_subject_type)
        and (source_video_id is None or asset.get("source_video_id") == source_video_id)
    ]
    if not assets:
        raise FileNotFoundError("no matching visual assets found; run `tapesplit extract-visuals` first")

    output_path = project / "visual_embeddings.jsonl"
    _remove_embedding_rows(output_path, source_video_id=source_video_id, subject_type=normalized_subject_type)
    next_index = _next_index(output_path, prefix="visual_embedding_")

    embedder = _create_visual_embedder(resolved_backend, model_name=model_name)
    scanned_assets = 0
    written = 0
    for asset in assets:
        image_path = project / str(asset.get("keyframe_path") or asset.get("thumbnail_path") or "")
        if not image_path.exists():
            continue
        scanned_assets += 1
        vector = embedder.embed_image(image_path)
        if not vector:
            continue
        written += 1
        append_jsonl(
            output_path,
            {
                "id": f"visual_embedding_{next_index + written - 1:06d}",
                "visual_asset_id": asset.get("id"),
                "source_video_id": asset.get("source_video_id"),
                "source_subject_type": asset.get("subject_type"),
                "source_subject_id": asset.get("subject_id"),
                "start_s": asset.get("start_s"),
                "end_s": asset.get("end_s"),
                "time_s": asset.get("time_s"),
                "source_image_path": str(asset.get("keyframe_path") or asset.get("thumbnail_path") or ""),
                "embedding_backend": resolved_backend,
                "embedding_model": embedder.model_name,
                "dim": len(vector),
                "vector": vector,
                "review_status": "unreviewed",
            },
        )

    return {
        "project": str(project),
        "output": str(output_path),
        "requested_backend": _normalize_visual_embedding_backend(backend),
        "resolved_backend": resolved_backend,
        "model": embedder.model_name,
        "source_video_id": source_video_id,
        "subject_type": normalized_subject_type or "all",
        "visual_assets_scanned": scanned_assets,
        "visual_embeddings": written,
    }


def build_visual_similarity_for_project(
    project_dir: Path,
    *,
    min_similarity: float = 0.82,
    limit_per_asset: int = 5,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if min_similarity < -1 or min_similarity > 1:
        raise ValueError("min_similarity must be between -1 and 1")
    if limit_per_asset <= 0:
        raise ValueError("limit_per_asset must be greater than 0")
    embeddings = read_jsonl(project / "visual_embeddings.jsonl")
    if not embeddings:
        raise FileNotFoundError("no visual embeddings found; run `tapesplit embed-visuals` first")

    output_path = project / "visual_similarity_edges.jsonl"
    if output_path.exists():
        output_path.unlink()

    vectors = [
        {
            **row,
            "_vector": _normalized_vector(row.get("vector") or []),
        }
        for row in embeddings
        if row.get("vector")
    ]
    pair_scores: dict[tuple[str, str], dict[str, Any]] = {}
    for left in vectors:
        scored = []
        for right in vectors:
            if left.get("id") == right.get("id"):
                continue
            score = _dot(left["_vector"], right["_vector"])
            if score >= min_similarity:
                scored.append((score, right))
        for score, right in sorted(scored, key=lambda item: item[0], reverse=True)[:limit_per_asset]:
            left_id = str(left.get("id") or "")
            right_id = str(right.get("id") or "")
            key = tuple(sorted([left_id, right_id]))
            if not key[0] or not key[1]:
                continue
            existing = pair_scores.get(key)
            if existing and existing["similarity"] >= score:
                continue
            pair_scores[key] = _visual_similarity_record(len(pair_scores) + 1, left, right, score)

    rows = sorted(
        pair_scores.values(),
        key=lambda row: (
            str(row.get("left_source_video_id") or ""),
            _number_or_large(row.get("left_time_s")),
            -float(row.get("similarity") or 0.0),
        ),
    )
    for index, row in enumerate(rows, start=1):
        row["id"] = f"visual_similarity_edge_{index:06d}"
        append_jsonl(output_path, row)

    return {
        "project": str(project),
        "output": str(output_path),
        "visual_embeddings": len(vectors),
        "visual_similarity_edges": len(rows),
        "min_similarity": min_similarity,
        "limit_per_asset": limit_per_asset,
    }


class _SentenceTransformerImageEmbedder:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        try:
            from PIL import Image  # type: ignore
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers and Pillow are required for visual embeddings. "
                "Install with `python -m pip install -e '.[visual-ai]'`."
            ) from exc
        self._image = Image
        self._model = SentenceTransformer(model_name)

    def embed_image(self, image_path: Path) -> list[float]:
        with self._image.open(image_path) as image:
            rgb = image.convert("RGB")
            vector = self._model.encode([rgb], normalize_embeddings=True, show_progress_bar=False)[0]
        return [round(float(value), 6) for value in vector]


def _create_visual_embedder(backend: str, *, model_name: str) -> Any:
    if backend == "sentence-transformers":
        return _SentenceTransformerImageEmbedder(model_name)
    raise ValueError(f"unsupported visual embedding backend: {backend}")


def _visual_similarity_record(index: int, left: dict[str, Any], right: dict[str, Any], score: float) -> dict[str, Any]:
    ordered = sorted(
        [left, right],
        key=lambda row: (
            str(row.get("source_video_id") or ""),
            _number_or_large(row.get("time_s")),
            str(row.get("id") or ""),
        ),
    )
    left, right = ordered[0], ordered[1]
    return {
        "id": f"visual_similarity_edge_{index:06d}",
        "left_visual_embedding_id": left.get("id"),
        "right_visual_embedding_id": right.get("id"),
        "left_visual_asset_id": left.get("visual_asset_id"),
        "right_visual_asset_id": right.get("visual_asset_id"),
        "left_source_subject_type": left.get("source_subject_type"),
        "right_source_subject_type": right.get("source_subject_type"),
        "left_source_subject_id": left.get("source_subject_id"),
        "right_source_subject_id": right.get("source_subject_id"),
        "left_source_video_id": left.get("source_video_id"),
        "right_source_video_id": right.get("source_video_id"),
        "left_time_s": left.get("time_s"),
        "right_time_s": right.get("time_s"),
        "similarity": round(float(score), 4),
        "predicate": "visually_similar_asset",
        "method": "local_visual_embedding_similarity",
        "embedding_model": left.get("embedding_model") or right.get("embedding_model"),
        "basis": [
            "local image embeddings are close",
            "candidate same-place, same-event, same-era, or repeated-visual-context evidence",
            "not a confirmed semantic match",
        ],
        "review_status": "unreviewed",
    }


def _dot(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=False))


def _normalized_vector(values: Any) -> list[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _number_or_large(value: Any) -> float:
    if value in (None, ""):
        return 1_000_000_000.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1_000_000_000.0


def _normalize_visual_embedding_backend(value: str) -> str:
    backend = value.strip().casefold().replace("_", "-")
    aliases = {"clip": "sentence-transformers", "st": "sentence-transformers"}
    backend = aliases.get(backend, backend)
    if backend not in VISUAL_EMBEDDING_BACKENDS:
        raise ValueError("visual embedding backend must be auto or sentence-transformers")
    return backend


def _resolve_visual_embedding_backend(value: str, *, require_available: bool = True) -> str:
    backend = _normalize_visual_embedding_backend(value)
    if backend == "auto":
        if _sentence_transformers_available():
            return "sentence-transformers"
        if require_available:
            raise RuntimeError(
                "No local visual embedding backend is available. Install with "
                "`python -m pip install -e '.[visual-ai]'`."
            )
        return "unavailable"
    if backend == "sentence-transformers" and not _sentence_transformers_available():
        if require_available:
            raise RuntimeError(
                "sentence-transformers is required for visual embeddings. Install with "
                "`python -m pip install -e '.[visual-ai]'`."
            )
        return "unavailable"
    return backend


def _sentence_transformers_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _remove_embedding_rows(path: Path, *, source_video_id: str | None, subject_type: str | None) -> None:
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

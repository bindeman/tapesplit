from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_VISUAL_CAPTION_BACKEND = "auto"
DEFAULT_VISUAL_CAPTION_MODEL = "Salesforce/blip-image-captioning-base"
VISUAL_CAPTION_BACKENDS = {"auto", "transformers-blip", "ollama"}


def check_visual_caption_config() -> dict[str, Any]:
    resolved = _resolve_visual_caption_backend("auto", require_available=False)
    return {
        "visual_caption_transformers": _transformers_caption_available(),
        "visual_caption_ollama": shutil.which("ollama") is not None,
        "visual_caption_default_backend": resolved,
        "visual_caption_default_model": DEFAULT_VISUAL_CAPTION_MODEL if resolved == "transformers-blip" else "",
    }


def caption_visual_assets_for_project(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    subject_type: str | None = None,
    backend: str = DEFAULT_VISUAL_CAPTION_BACKEND,
    model_name: str = DEFAULT_VISUAL_CAPTION_MODEL,
    prompt: str = "Describe the home-video frame, including setting, event, visible people, signs, and whether it looks like family footage.",
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    resolved_backend = _resolve_visual_caption_backend(backend)
    normalized_subject_type = _normalize_subject_type(subject_type)
    assets = [
        asset
        for asset in read_jsonl(project / "visual_assets.jsonl")
        if (normalized_subject_type is None or asset.get("subject_type") == normalized_subject_type)
        and (source_video_id is None or asset.get("source_video_id") == source_video_id)
    ]
    if not assets:
        raise FileNotFoundError("no matching visual assets found; run `tapesplit extract-visuals` first")

    output_path = project / "visual_captions.jsonl"
    _remove_caption_rows(output_path, source_video_id=source_video_id, subject_type=normalized_subject_type)
    next_index = _next_index(output_path, prefix="visual_caption_")
    captioner = _create_captioner(resolved_backend, model_name=model_name, prompt=prompt)

    scanned = 0
    written = 0
    for asset in assets:
        image_path = project / str(asset.get("keyframe_path") or asset.get("thumbnail_path") or "")
        if not image_path.exists():
            continue
        scanned += 1
        caption = captioner.caption(image_path)
        if not caption:
            continue
        written += 1
        append_jsonl(
            output_path,
            {
                "id": f"visual_caption_{next_index + written - 1:06d}",
                "visual_asset_id": asset.get("id"),
                "source_video_id": asset.get("source_video_id"),
                "source_subject_type": asset.get("subject_type"),
                "source_subject_id": asset.get("subject_id"),
                "start_s": asset.get("start_s"),
                "end_s": asset.get("end_s"),
                "time_s": asset.get("time_s"),
                "caption": caption,
                "caption_backend": resolved_backend,
                "caption_model": captioner.model_name,
                "prompt": prompt if resolved_backend == "ollama" else "",
                "source_image_path": str(asset.get("keyframe_path") or asset.get("thumbnail_path") or ""),
                "review_status": "unreviewed",
            },
        )

    return {
        "project": str(project),
        "output": str(output_path),
        "requested_backend": _normalize_visual_caption_backend(backend),
        "resolved_backend": resolved_backend,
        "model": captioner.model_name,
        "source_video_id": source_video_id,
        "subject_type": normalized_subject_type or "all",
        "visual_assets_scanned": scanned,
        "visual_captions": written,
    }


class _TransformersBlipCaptioner:
    def __init__(self, model_name: str, _prompt: str) -> None:
        self.model_name = model_name
        try:
            from PIL import Image  # type: ignore
            from transformers import BlipForConditionalGeneration, BlipProcessor  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "transformers and Pillow are required for local visual captions. "
                "Install with `python -m pip install -e '.[visual-ai]'`."
            ) from exc
        self._image = Image
        self._processor = BlipProcessor.from_pretrained(model_name)
        self._model = BlipForConditionalGeneration.from_pretrained(model_name)

    def caption(self, image_path: Path) -> str:
        with self._image.open(image_path) as image:
            inputs = self._processor(image.convert("RGB"), return_tensors="pt")
            output = self._model.generate(**inputs, max_new_tokens=40)
        return str(self._processor.decode(output[0], skip_special_tokens=True)).strip()


class _OllamaCaptioner:
    def __init__(self, model_name: str, prompt: str) -> None:
        self.model_name = model_name
        self._prompt = prompt

    def caption(self, image_path: Path) -> str:
        command = ["ollama", "run", self.model_name, self._prompt, str(image_path)]
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return " ".join(completed.stdout.split())


def _create_captioner(backend: str, *, model_name: str, prompt: str) -> Any:
    if backend == "transformers-blip":
        return _TransformersBlipCaptioner(model_name, prompt)
    if backend == "ollama":
        return _OllamaCaptioner(model_name, prompt)
    raise ValueError(f"unsupported visual caption backend: {backend}")


def _normalize_visual_caption_backend(value: str) -> str:
    backend = value.strip().casefold().replace("_", "-")
    aliases = {"blip": "transformers-blip", "transformers": "transformers-blip"}
    backend = aliases.get(backend, backend)
    if backend not in VISUAL_CAPTION_BACKENDS:
        raise ValueError("visual caption backend must be auto, transformers-blip, or ollama")
    return backend


def _resolve_visual_caption_backend(value: str, *, require_available: bool = True) -> str:
    backend = _normalize_visual_caption_backend(value)
    if backend == "auto":
        if _transformers_caption_available():
            return "transformers-blip"
        if shutil.which("ollama"):
            return "ollama"
        if require_available:
            raise RuntimeError(
                "No local visual caption backend is available. Install `.[visual-ai]` or Ollama."
            )
        return "unavailable"
    if backend == "transformers-blip" and not _transformers_caption_available():
        if require_available:
            raise RuntimeError(
                "transformers and Pillow are required for local visual captions. "
                "Install with `python -m pip install -e '.[visual-ai]'`."
            )
        return "unavailable"
    if backend == "ollama" and not shutil.which("ollama"):
        if require_available:
            raise RuntimeError("Ollama is not installed or not on PATH")
        return "unavailable"
    return backend


def _transformers_caption_available() -> bool:
    try:
        import PIL  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def _remove_caption_rows(path: Path, *, source_video_id: str | None, subject_type: str | None) -> None:
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

from __future__ import annotations

import json
import os
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from tapesplit.env import load_dotenv
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.transcription import extract_project_audio


SUPPORTED_SPEAKER_FORMATS = {"json", "rttm"}
DEFAULT_SPEAKER_DIARIZATION_BACKEND = "auto"
DEFAULT_SPEAKER_DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
DEFAULT_TRANSCRIPT_EMBEDDING_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
TRANSCRIPT_EMBEDDING_BACKEND = "transcript-embedding"
TRANSCRIPT_EMBEDDING_STRIDE_S = 10.0
TRANSCRIPT_EMBEDDING_CLUSTER_DISTANCE = 0.9
AZURE_OPENAI_DIARIZE_BACKEND = "azure-openai"
# 20-minute parts stay well under the service's 25MB upload cap at 48kbps
# mono mp3 (~7.2MB); 30s overlap lets the seam be reconciled by midpoint.
AZURE_DIARIZE_PART_SECONDS = 1200.0
AZURE_DIARIZE_OVERLAP_SECONDS = 30.0
AZURE_DIARIZE_MAX_REFERENCES = 4
AZURE_DIARIZE_REFERENCE_MIN_S = 2.5
AZURE_DIARIZE_REFERENCE_MAX_S = 9.5
AZURE_DIARIZE_REFERENCE_MIN_CONFIDENCE = 0.7

_RTTM_RE = re.compile(r"\s+")


def check_speaker_diarization_config() -> dict[str, Any]:
    load_dotenv()
    return {
        "speaker_diarization_pyannote": _pyannote_available(),
        "speaker_diarization_speechbrain": _speechbrain_available(),
        "speaker_diarization_hf_token": bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")),
        "speaker_diarization_azure_openai": _azure_diarize_selected(),
        "speaker_diarization_default_backend": DEFAULT_SPEAKER_DIARIZATION_BACKEND,
        "speaker_diarization_default_model": DEFAULT_SPEAKER_DIARIZATION_MODEL,
        "speaker_diarization_transcript_embedding_model": DEFAULT_TRANSCRIPT_EMBEDDING_MODEL,
    }


def _azure_diarize_selected() -> bool:
    """Cloud diarization is opt-in: it spends credits, so `auto` only routes
    to it when TAPESPLIT_DIARIZE_BACKEND explicitly selects it AND the Azure
    OpenAI env is configured."""
    if (os.environ.get("TAPESPLIT_DIARIZE_BACKEND") or "").strip() != AZURE_OPENAI_DIARIZE_BACKEND:
        return False
    from tapesplit.azure_openai_adapter import load_azure_openai_config

    return load_azure_openai_config().configured


def diarize_project_speakers(
    project_dir: Path,
    *,
    source_video_id: str | None = None,
    backend: str = DEFAULT_SPEAKER_DIARIZATION_BACKEND,
    model_name: str = DEFAULT_SPEAKER_DIARIZATION_MODEL,
    force: bool = False,
) -> dict[str, Any]:
    load_dotenv()
    project = project_dir.expanduser().resolve()
    if backend == "auto" and _azure_diarize_selected():
        backend = AZURE_OPENAI_DIARIZE_BACKEND
    if backend == AZURE_OPENAI_DIARIZE_BACKEND:
        return _diarize_project_speakers_azure_openai(
            project,
            source_video_id=source_video_id,
            force=force,
        )
    if backend == "auto":
        pyannote_error: Exception | None = None
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        if _pyannote_available():
            if _pyannote_model_accessible(model_name, token=token):
                try:
                    return _diarize_project_speakers_pyannote(
                        project,
                        source_video_id=source_video_id,
                        model_name=model_name,
                        force=force,
                    )
                except Exception as exc:
                    pyannote_error = exc
            else:
                pyannote_error = RuntimeError(
                    f"HF token does not have access to pyannote diarization model {model_name}"
                )
        if _speechbrain_available():
            return _diarize_project_speakers_transcript_embeddings(
                project,
                source_video_id=source_video_id,
                model_name=DEFAULT_TRANSCRIPT_EMBEDDING_MODEL,
                force=force,
                fallback_error=pyannote_error,
            )
        if pyannote_error:
            raise pyannote_error
        raise RuntimeError(
            "no speaker diarization backend is available. Install pyannote.audio or speechbrain."
        )
    if backend == "pyannote":
        return _diarize_project_speakers_pyannote(
            project,
            source_video_id=source_video_id,
            model_name=model_name,
            force=force,
        )
    if backend in {TRANSCRIPT_EMBEDDING_BACKEND, "speechbrain"}:
        selected_model = model_name
        if selected_model == DEFAULT_SPEAKER_DIARIZATION_MODEL:
            selected_model = DEFAULT_TRANSCRIPT_EMBEDDING_MODEL
        return _diarize_project_speakers_transcript_embeddings(
            project,
            source_video_id=source_video_id,
            model_name=selected_model,
            force=force,
        )
    raise ValueError(
        "speaker diarization backend must be one of: auto, pyannote, transcript-embedding, azure-openai"
    )


def _diarize_project_speakers_pyannote(
    project: Path,
    *,
    source_video_id: str | None,
    model_name: str,
    force: bool,
) -> dict[str, Any]:
    if not _pyannote_available():
        raise RuntimeError("pyannote.audio is not installed. Install a diarization extra or import RTTM/JSON instead.")

    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyannote.audio is not installed") from exc

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    pipeline = Pipeline.from_pretrained(model_name, token=token)
    if pipeline is None:
        raise RuntimeError(
            f"could not load speaker diarization model {model_name}. "
            "Confirm the HF token has access and the model conditions are accepted."
        )
    audio = extract_project_audio(project, source_video_id=source_video_id, force=False)
    run_id = f"spk_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    all_segments = []
    for item in audio["audio_files"]:
        diarization = pipeline(item["audio_path"])
        for turn, _track, speaker in diarization.itertracks(yield_label=True):
            all_segments.append(
                {
                    "source_video_id": item["source_video_id"],
                    "start_s": round(float(turn.start), 3),
                    "end_s": round(float(turn.end), 3),
                    "speaker_label": str(speaker),
                    "confidence": None,
                    "provider": "pyannote",
                    "model": model_name,
                    "metadata": {"run_id": run_id, "audio_path": item["audio_path"]},
                }
            )
    written = write_speaker_segments(project, all_segments, force=force, source_video_id=source_video_id)
    append_jsonl(
        project / "speaker_runs.jsonl",
        {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": "pyannote",
            "model": model_name,
            "source_video_id": source_video_id,
            "segments": written,
        },
    )
    return {
        "project": str(project),
        "run_id": run_id,
        "backend": backend,
        "model": model_name,
        "speaker_segments": written,
        "output": str(project / "speaker_segments.jsonl"),
    }


def _diarize_project_speakers_transcript_embeddings(
    project: Path,
    *,
    source_video_id: str | None,
    model_name: str,
    force: bool,
    fallback_error: Exception | None = None,
) -> dict[str, Any]:
    if not _speechbrain_available():
        raise RuntimeError("speechbrain is not installed. Install a diarization extra or use pyannote/import.")

    try:
        import numpy as np  # type: ignore
        import torch  # type: ignore
        import torchaudio  # type: ignore
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore
    except ImportError as exc:
        raise RuntimeError("speechbrain diarization requires numpy, torch, torchaudio, and speechbrain") from exc

    transcript_rows = [
        row
        for row in read_jsonl(project / "transcript_segments.jsonl")
        if row.get("source_video_id") and (not source_video_id or row.get("source_video_id") == source_video_id)
    ]
    if not transcript_rows:
        raise RuntimeError(
            "transcript-embedding diarization needs transcript_segments.jsonl. "
            "Run local transcription first or use the pyannote backend."
        )

    audio = extract_project_audio(project, source_video_id=source_video_id, force=False)
    audio_by_source = {item["source_video_id"]: Path(item["audio_path"]) for item in audio["audio_files"]}
    cache_dir = project / ".cache" / "speechbrain" / _safe_model_dir(model_name)
    classifier = EncoderClassifier.from_hparams(source=model_name, savedir=str(cache_dir), run_opts={"device": "cpu"})

    run_id = f"spk_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    all_segments: list[dict[str, Any]] = []
    total_anchors = 0
    for current_source_id, rows in _group_transcript_rows_by_source(transcript_rows).items():
        audio_path = audio_by_source.get(current_source_id)
        if not audio_path:
            continue
        signal, sample_rate = torchaudio.load(str(audio_path))
        if signal.ndim == 2:
            signal = signal.mean(dim=0)
        if int(sample_rate) != 16000:
            signal = torchaudio.functional.resample(signal, int(sample_rate), 16000)
            sample_rate = 16000
        audio_duration = float(signal.shape[-1]) / float(sample_rate or 16000)
        sorted_rows = sorted(rows, key=lambda item: float(item.get("start_s") or 0.0))
        anchor_rows = _sample_transcript_rows_for_speaker_embeddings(sorted_rows)
        embedded_rows = []
        clips = []
        clip_lengths = []
        for row in anchor_rows:
            window = _speaker_embedding_window(row, audio_duration=audio_duration)
            if not window:
                continue
            start_sample = max(0, int(window[0] * sample_rate))
            end_sample = min(signal.shape[-1], int(window[1] * sample_rate))
            clip = signal[start_sample:end_sample]
            if clip.numel() < int(0.4 * sample_rate):
                continue
            embedded_rows.append(row)
            clips.append(clip)
            clip_lengths.append(int(clip.numel()))
        if not clips:
            continue
        cache_path = _speaker_embedding_cache_path(project, current_source_id, model_name)
        cached_embeddings = _load_speaker_embedding_cache(cache_path, embedded_rows, model_name)
        if cached_embeddings is None:
            embeddings = _encode_speechbrain_embeddings(classifier, clips, clip_lengths, torch=torch)
            _write_speaker_embedding_cache(cache_path, embedded_rows, embeddings, model_name)
        else:
            embeddings = cached_embeddings
        labels = _cluster_voice_embeddings(embeddings)
        canonical_labels = _canonicalize_cluster_labels(labels)
        confidences = _cluster_confidences(embeddings, canonical_labels)
        total_anchors += len(embedded_rows)
        labeled_rows = _label_transcript_rows_from_speaker_anchors(
            sorted_rows,
            embedded_rows,
            canonical_labels,
            confidences,
            source_video_id=current_source_id,
            model_name=model_name,
            run_id=run_id,
            audio_path=audio_path,
        )
        all_segments.extend(_merge_adjacent_speaker_segments(labeled_rows))

    written = write_speaker_segments(project, all_segments, force=force, source_video_id=source_video_id)
    run_record = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": TRANSCRIPT_EMBEDDING_BACKEND,
        "model": model_name,
        "source_video_id": source_video_id,
        "segments": written,
        "metadata": {
            "embedding_anchor_segments": total_anchors,
            "embedding_stride_s": TRANSCRIPT_EMBEDDING_STRIDE_S,
            "cluster_distance": _speaker_cluster_distance_threshold(),
        },
    }
    if fallback_error:
        run_record["metadata"] = {
            **run_record["metadata"],
            "fallback_from": "pyannote",
            "fallback_error_type": type(fallback_error).__name__,
            "fallback_error": str(fallback_error).splitlines()[0] if str(fallback_error).splitlines() else "",
        }
    append_jsonl(project / "speaker_runs.jsonl", run_record)
    return {
        "project": str(project),
        "run_id": run_id,
        "backend": TRANSCRIPT_EMBEDDING_BACKEND,
        "model": model_name,
        "speaker_segments": written,
        "output": str(project / "speaker_segments.jsonl"),
    }


def _diarize_project_speakers_azure_openai(
    project: Path,
    *,
    source_video_id: str | None,
    force: bool,
    deployment: str | None = None,
) -> dict[str, Any]:
    from tapesplit import azure_openai_adapter

    config = azure_openai_adapter.load_azure_openai_config()
    if not config.configured:
        raise RuntimeError(
            "azure-openai diarization needs AZURE_OPENAI_API_KEY, "
            "AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_API_BASE), and AZURE_OPENAI_API_VERSION"
        )
    selected_deployment = deployment or os.environ.get(
        "TAPESPLIT_DIARIZE_DEPLOYMENT", azure_openai_adapter.DEFAULT_DIARIZE_DEPLOYMENT
    )

    audio = extract_project_audio(project, source_video_id=source_video_id, force=False)
    references = _mine_speaker_reference_clips(project)
    reference_clips = {
        name: Path(clip_path).read_bytes() for name, clip_path in references.items()
    }
    run_id = f"spk_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    all_segments: list[dict[str, Any]] = []
    part_counts: dict[str, int] = {}
    for item in audio["audio_files"]:
        current_source_id = item["source_video_id"]
        audio_path = Path(item["audio_path"])
        duration = _audio_duration_s(audio_path)
        parts = _plan_audio_parts(
            duration,
            part_s=AZURE_DIARIZE_PART_SECONDS,
            overlap_s=AZURE_DIARIZE_OVERLAP_SECONDS,
        )
        part_counts[current_source_id] = len(parts)
        raw_by_part: list[list[dict[str, Any]]] = []
        for part_index, (part_start, part_end) in enumerate(parts):
            part_path = _extract_audio_part_mp3(audio_path, part_start, part_end - part_start)
            try:
                response = azure_openai_adapter.transcribe_diarize(
                    audio_path=part_path,
                    deployment=selected_deployment,
                    known_speakers=reference_clips or None,
                    project_dir=project,
                )
            finally:
                part_path.unlink(missing_ok=True)
            rows = []
            for segment in response.get("segments") or []:
                start = _number_or_none(segment.get("start"))
                end = _number_or_none(segment.get("end"))
                if start is None or end is None or end <= start:
                    continue
                rows.append(
                    {
                        "part_index": part_index,
                        "start_s": round(start + part_start, 3),
                        "end_s": round(end + part_start, 3),
                        "raw_speaker": str(segment.get("speaker") or ""),
                        "text": segment.get("text") or "",
                    }
                )
            raw_by_part.append(rows)

        kept = _reconcile_azure_parts(raw_by_part, parts)
        label_map = _unify_unnamed_azure_labels(
            kept,
            known_names=set(reference_clips),
            embed_fn=_azure_segment_embed_fn(project, current_source_id, audio_path),
        )
        for row in kept:
            key = (row["part_index"], row["raw_speaker"])
            label = label_map.get(key, row["raw_speaker"])
            all_segments.append(
                {
                    "source_video_id": current_source_id,
                    "start_s": row["start_s"],
                    "end_s": row["end_s"],
                    "speaker_label": label,
                    "confidence": None,
                    "provider": "azure_openai",
                    "model": selected_deployment,
                    "metadata": {
                        "run_id": run_id,
                        "part_index": row["part_index"],
                        "raw_speaker": row["raw_speaker"],
                        "known_speaker": row["raw_speaker"] in reference_clips,
                        "transcript_text": row["text"],
                    },
                }
            )

    written = write_speaker_segments(project, all_segments, force=force, source_video_id=source_video_id)
    append_jsonl(
        project / "speaker_runs.jsonl",
        {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": "azure_openai",
            "model": selected_deployment,
            "source_video_id": source_video_id,
            "segments": written,
            "metadata": {
                "parts_by_source": part_counts,
                "known_speaker_names": sorted(reference_clips),
                "part_seconds": AZURE_DIARIZE_PART_SECONDS,
                "overlap_seconds": AZURE_DIARIZE_OVERLAP_SECONDS,
            },
        },
    )
    return {
        "project": str(project),
        "run_id": run_id,
        "backend": AZURE_OPENAI_DIARIZE_BACKEND,
        "model": selected_deployment,
        "speaker_segments": written,
        "output": str(project / "speaker_segments.jsonl"),
    }


def _plan_audio_parts(
    duration_s: float,
    *,
    part_s: float = AZURE_DIARIZE_PART_SECONDS,
    overlap_s: float = AZURE_DIARIZE_OVERLAP_SECONDS,
) -> list[tuple[float, float]]:
    if duration_s <= 0:
        return []
    if duration_s <= part_s:
        return [(0.0, round(duration_s, 3))]
    stride = part_s - overlap_s
    parts: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(start + part_s, duration_s)
        parts.append((round(start, 3), round(end, 3)))
        if end >= duration_s:
            break
        start += stride
    return parts


def _reconcile_azure_parts(
    raw_by_part: list[list[dict[str, Any]]],
    parts: list[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Drop duplicate segments in overlap zones: each absolute instant is
    owned by exactly one part (overlaps split at their midpoint), and a
    segment survives iff its midpoint falls in its own part's zone."""
    kept: list[dict[str, Any]] = []
    for index, rows in enumerate(raw_by_part):
        zone_start = parts[index][0]
        zone_end = parts[index][1]
        if index > 0:
            zone_start = parts[index][0] + (parts[index - 1][1] - parts[index][0]) / 2.0
        if index < len(parts) - 1:
            zone_end = parts[index + 1][0] + (parts[index][1] - parts[index + 1][0]) / 2.0
        for row in rows:
            midpoint = (float(row["start_s"]) + float(row["end_s"])) / 2.0
            if zone_start <= midpoint < zone_end or (
                index == len(parts) - 1 and midpoint >= zone_start
            ):
                kept.append(row)
    return sorted(kept, key=lambda item: float(item["start_s"]))


def _unify_unnamed_azure_labels(
    rows: list[dict[str, Any]],
    *,
    known_names: set[str],
    embed_fn: Any,
) -> dict[tuple[int, str], str]:
    """Map per-part anonymous labels (A/B/C restart every request) to stable
    per-tape labels. Named speakers (from reference clips) are already stable
    and pass through. Unnamed (part, label) groups are voice-embedded via
    `embed_fn(windows) -> vectors` and clustered; on failure each group keeps
    a part-scoped label so no segments are lost."""
    groups: dict[tuple[int, str], list[tuple[float, float]]] = {}
    for row in rows:
        key = (int(row["part_index"]), str(row["raw_speaker"]))
        if key[1] in known_names:
            continue
        duration = float(row["end_s"]) - float(row["start_s"])
        if 1.0 <= duration <= 12.0:
            groups.setdefault(key, []).append((float(row["start_s"]), float(row["end_s"])))
        else:
            groups.setdefault(key, [])

    mapping: dict[tuple[int, str], str] = {}
    for row in rows:
        key = (int(row["part_index"]), str(row["raw_speaker"]))
        if key[1] in known_names:
            mapping[key] = key[1]

    keys = sorted(groups)
    if not keys:
        return mapping
    if len({key[0] for key in keys}) <= 1:
        for key in keys:
            mapping[key] = f"AZ_SPEAKER_{_azure_label_ordinal(key[1]):02d}"
        return mapping

    centroids = []
    embeddable_keys = []
    for key in keys:
        windows = sorted(groups[key], key=lambda item: item[1] - item[0], reverse=True)[:3]
        if not windows:
            continue
        try:
            vectors = embed_fn(windows)
        except Exception:
            vectors = None
        if vectors is None or len(vectors) == 0:
            continue
        centroids.append(_mean_unit_vector(vectors))
        embeddable_keys.append(key)

    if len(embeddable_keys) >= 2:
        labels = _cluster_voice_embeddings(centroids)
        canonical = _canonicalize_cluster_labels(labels)
        for key, label in zip(embeddable_keys, canonical, strict=False):
            mapping[key] = f"AZ_SPEAKER_{int(label):02d}"
    for key in keys:
        if key not in mapping:
            mapping[key] = f"AZ_P{key[0]:02d}_{key[1]}"
    return mapping


def _azure_label_ordinal(label: str) -> int:
    if len(label) == 1 and label.isalpha():
        return ord(label.upper()) - ord("A")
    digits = re.sub(r"\D", "", label)
    return int(digits) if digits else 0


def _mean_unit_vector(vectors: Any) -> list[float]:
    import numpy as np  # type: ignore

    matrix = np.asarray(vectors, dtype="float32")
    centroid = matrix.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    return [float(value) for value in centroid / max(norm, 1e-12)]


def _azure_segment_embed_fn(project: Path, source_video_id: str, audio_path: Path) -> Any:
    """Returns embed_fn(windows) -> vectors using the speechbrain encoder
    already used by the transcript-embedding backend; None-returning closure
    when speechbrain is unavailable (callers then keep part-scoped labels)."""
    if not _speechbrain_available():
        return lambda windows: None

    def embed(windows: list[tuple[float, float]]) -> Any:
        import torch  # type: ignore
        import torchaudio  # type: ignore
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore

        model_name = DEFAULT_TRANSCRIPT_EMBEDDING_MODEL
        cache_dir = project / ".cache" / "speechbrain" / _safe_model_dir(model_name)
        classifier = EncoderClassifier.from_hparams(
            source=model_name, savedir=str(cache_dir), run_opts={"device": "cpu"}
        )
        signal, sample_rate = torchaudio.load(str(audio_path))
        if signal.ndim == 2:
            signal = signal.mean(dim=0)
        if int(sample_rate) != 16000:
            signal = torchaudio.functional.resample(signal, int(sample_rate), 16000)
            sample_rate = 16000
        clips = []
        lengths = []
        for start, end in windows:
            start_sample = max(0, int(start * sample_rate))
            end_sample = min(signal.shape[-1], int(end * sample_rate))
            clip = signal[start_sample:end_sample]
            if clip.numel() < int(0.4 * sample_rate):
                continue
            clips.append(clip)
            lengths.append(int(clip.numel()))
        if not clips:
            return None
        return _encode_speechbrain_embeddings(classifier, clips, lengths, torch=torch)

    return embed


def _mine_speaker_reference_clips(project: Path) -> dict[str, str]:
    """Pick <=4 named reference clips (2.5-9.5s) from person-linked speaker
    segments so the cloud diarizer emits real names instead of A/B/C. Mined
    once per project and cached: the segments being replaced by this backend
    are exactly the ones the references come from."""
    manifest_path = project / ".cache" / "azure_diarize_refs" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        clips = {
            name: path for name, path in (manifest.get("clips") or {}).items() if Path(path).exists()
        }
        if clips:
            return clips

    people_labels: dict[str, str] = {}
    for row in read_jsonl(project / "people_groups.jsonl"):
        label = str(row.get("label") or row.get("display_label") or "")
        if row.get("id") and label:
            people_labels[str(row["id"])] = label

    candidates: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for row in read_jsonl(project / "speaker_segments.jsonl"):
        person_id = row.get("person_group_id")
        if not person_id or person_id not in people_labels:
            continue
        name = _reference_display_name(people_labels[str(person_id)])
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        confidence = _number_or_none(row.get("confidence")) or 0.0
        duration = _row_duration(row)
        if confidence < AZURE_DIARIZE_REFERENCE_MIN_CONFIDENCE:
            continue
        if not (AZURE_DIARIZE_REFERENCE_MIN_S <= duration <= AZURE_DIARIZE_REFERENCE_MAX_S):
            continue
        candidates.setdefault(name, []).append(row)

    audio_dir = project / "audio"
    clips: dict[str, str] = {}
    output_dir = manifest_path.parent
    for name in sorted(candidates, key=lambda item: counts.get(item, 0), reverse=True):
        if len(clips) >= AZURE_DIARIZE_MAX_REFERENCES:
            break
        best = max(
            candidates[name],
            key=lambda row: (
                _number_or_none(row.get("confidence")) or 0.0,
                -abs(_row_duration(row) - 6.0),
            ),
        )
        wav_path = audio_dir / f"{best.get('source_video_id')}.wav"
        if not wav_path.exists():
            continue
        output_dir.mkdir(parents=True, exist_ok=True)
        clip_path = output_dir / f"{_safe_model_dir(name)}.mp3"
        try:
            _extract_audio_clip_mp3(
                wav_path,
                float(best.get("start_s") or 0.0),
                _row_duration(best),
                clip_path,
            )
        except Exception:
            continue
        clips[name] = str(clip_path)

    if clips:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({"clips": clips}, sort_keys=True) + "\n", encoding="utf-8"
        )
    return clips


def _reference_display_name(label: str) -> str:
    for alias in label.split("/"):
        cleaned = alias.strip()
        if cleaned and all(ord(char) < 128 for char in cleaned):
            return cleaned
    return label.split("/")[0].strip()


def _audio_duration_s(audio_path: Path) -> float:
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _extract_audio_part_mp3(audio_path: Path, start_s: float, duration_s: float) -> Path:
    import subprocess
    import tempfile

    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    handle.close()
    output = Path(handle.name)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start_s:.3f}",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(output),
        ],
        capture_output=True,
        check=True,
    )
    return output


def _extract_audio_clip_mp3(audio_path: Path, start_s: float, duration_s: float, output: Path) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start_s:.3f}",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(output),
        ],
        capture_output=True,
        check=True,
    )


def import_speaker_segments(
    project_dir: Path,
    speaker_path: Path,
    *,
    source_video_id: str,
    speaker_format: str = "auto",
    offset_seconds: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    source_id = _validate_source_video_id(project, source_video_id)
    path = speaker_path.expanduser().resolve()
    segments = parse_speaker_file(path, speaker_format=speaker_format)
    for segment in segments:
        segment["source_video_id"] = source_id
        segment["start_s"] = round(float(segment.get("start_s") or 0.0) + offset_seconds, 3)
        segment["end_s"] = round(float(segment.get("end_s") or segment["start_s"]) + offset_seconds, 3)
        segment.setdefault("provider", "imported")
        segment.setdefault("model", "")
        segment.setdefault("metadata", {})
        segment["metadata"] = {**segment["metadata"], "source_file": str(path)}
    written = write_speaker_segments(project, segments, force=force, source_video_id=source_id)
    return {
        "project": str(project),
        "source_video_id": source_id,
        "speaker_segments": written,
        "output": str(project / "speaker_segments.jsonl"),
    }


def parse_speaker_file(path: Path, *, speaker_format: str = "auto") -> list[dict[str, Any]]:
    resolved = _resolve_speaker_format(path, speaker_format)
    if resolved == "json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("segments") if isinstance(payload, dict) else payload
        return [_normalize_speaker_segment(row) for row in rows or [] if isinstance(row, dict)]
    if resolved == "rttm":
        return _parse_rttm(path.read_text(encoding="utf-8"))
    raise ValueError(f"unsupported speaker format: {resolved}")


def write_speaker_segments(
    project: Path,
    segments: list[dict[str, Any]],
    *,
    force: bool,
    source_video_id: str | None = None,
) -> int:
    output = project / "speaker_segments.jsonl"
    existing = read_jsonl(output)
    normalized_new = [
        _normalize_speaker_segment(segment)
        for segment in segments
        if segment.get("source_video_id") and segment.get("speaker_label")
    ]
    new_source_ids = {row.get("source_video_id") for row in normalized_new if row.get("source_video_id")}
    if source_video_id and any(row.get("source_video_id") == source_video_id for row in existing) and not force:
        raise FileExistsError(
            f"speaker segments already exist for {source_video_id}; pass --force to replace them"
        )
    kept = [
        row
        for row in existing
        if row.get("source_video_id") not in (new_source_ids if not source_video_id else {source_video_id})
    ]
    rows = kept + normalized_new
    if output.exists():
        output.unlink()
    for index, row in enumerate(rows, start=1):
        append_jsonl(output, {"id": f"speaker_segment_{index:06d}", **row})
    return len(normalized_new)


def _parse_rttm(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        parts = _RTTM_RE.split(cleaned)
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        start_s = float(parts[3])
        duration_s = float(parts[4])
        rows.append(
            {
                "start_s": round(start_s, 3),
                "end_s": round(start_s + duration_s, 3),
                "speaker_label": parts[7],
                "confidence": None,
                "provider": "rttm",
                "model": "",
                "metadata": {"rttm_file_id": parts[1], "channel": parts[2]},
            }
        )
    return rows


def _normalize_speaker_segment(segment: dict[str, Any]) -> dict[str, Any]:
    start_s = _number_or_none(segment.get("start_s") or segment.get("start") or segment.get("begin")) or 0.0
    end_s = _number_or_none(segment.get("end_s") or segment.get("end"))
    if end_s is None:
        duration = _number_or_none(segment.get("duration_s") or segment.get("duration")) or 0.0
        end_s = start_s + duration
    speaker = segment.get("speaker_label") or segment.get("speaker") or segment.get("label")
    return {
        "source_video_id": str(segment.get("source_video_id") or ""),
        "start_s": round(float(start_s), 3),
        "end_s": round(float(end_s), 3),
        "speaker_label": str(speaker or ""),
        "confidence": segment.get("confidence"),
        "provider": segment.get("provider") or "imported",
        "model": segment.get("model") or "",
        "metadata": segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {},
        "review_status": segment.get("review_status") or "unreviewed",
    }


def _resolve_speaker_format(path: Path, requested: str) -> str:
    if requested != "auto":
        if requested not in SUPPORTED_SPEAKER_FORMATS:
            raise ValueError(f"unsupported speaker format: {requested}")
        return requested
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".rttm":
        return "rttm"
    raise ValueError(f"could not infer speaker format from {path.name}")


def _validate_source_video_id(project: Path, source_video_id: str) -> str:
    ids = {str(row.get("id")) for row in read_jsonl(project / "tapes.jsonl") if row.get("id")}
    if source_video_id not in ids:
        raise ValueError(f"source video id not found in project: {source_video_id}")
    return source_video_id


def _pyannote_available() -> bool:
    try:
        import pyannote.audio  # type: ignore  # noqa: F401
    except ImportError:
        return False
    return True


def _pyannote_model_accessible(model_name: str, *, token: str | None) -> bool:
    if Path(model_name).expanduser().exists():
        return True
    try:
        from huggingface_hub import hf_hub_download  # type: ignore

        hf_hub_download(model_name, "config.yaml", token=token)
    except Exception:
        return False
    return True


def _speechbrain_available() -> bool:
    try:
        import speechbrain  # type: ignore  # noqa: F401
    except ImportError:
        return False
    return True


def _group_transcript_rows_by_source(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("source_video_id")), []).append(row)
    return grouped


def _sample_transcript_rows_for_speaker_embeddings(
    rows: list[dict[str, Any]],
    *,
    stride_s: float = TRANSCRIPT_EMBEDDING_STRIDE_S,
) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        start = _number_or_none(row.get("start_s")) or 0.0
        end = _number_or_none(row.get("end_s"))
        if end is None or end <= start:
            continue
        bucket = int(_row_midpoint(row) // max(stride_s, 1.0))
        existing = buckets.get(bucket)
        if existing is None or _row_duration(row) > _row_duration(existing):
            buckets[bucket] = row
    return [buckets[key] for key in sorted(buckets)]


def _speaker_embedding_window(row: dict[str, Any], *, audio_duration: float) -> tuple[float, float] | None:
    start = _number_or_none(row.get("start_s")) or 0.0
    end = _number_or_none(row.get("end_s"))
    if end is None or end <= start:
        return None
    center = (start + end) / 2.0
    raw_span = end - start
    span = min(2.4, max(1.0, raw_span + 0.15))
    window_start = max(0.0, center - span / 2.0)
    window_end = min(audio_duration, window_start + span)
    if window_end - window_start < span:
        window_start = max(0.0, window_end - span)
    if window_end <= window_start:
        return None
    return round(window_start, 3), round(window_end, 3)


def _label_transcript_rows_from_speaker_anchors(
    rows: list[dict[str, Any]],
    anchor_rows: list[dict[str, Any]],
    anchor_labels: list[int],
    anchor_confidences: list[float],
    *,
    source_video_id: str,
    model_name: str,
    run_id: str,
    audio_path: Path,
) -> list[dict[str, Any]]:
    if not anchor_rows:
        return []
    anchors = sorted(
        [
            {
                "midpoint": _row_midpoint(row),
                "row": row,
                "label": label,
                "confidence": confidence,
            }
            for row, label, confidence in zip(anchor_rows, anchor_labels, anchor_confidences, strict=False)
        ],
        key=lambda item: float(item["midpoint"]),
    )
    anchor_midpoints = [float(anchor["midpoint"]) for anchor in anchors]
    labeled_rows = []
    for row in rows:
        midpoint = _row_midpoint(row)
        nearest = _nearest_anchor(anchors, anchor_midpoints, midpoint)
        if nearest is None:
            continue
        distance_s = abs(midpoint - float(nearest["midpoint"]))
        confidence = max(0.2, float(nearest["confidence"]) - min(0.25, distance_s / 180.0))
        anchor_row = nearest["row"]
        labeled_rows.append(
            {
                "source_video_id": source_video_id,
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
                "speaker_label": f"LOCAL_SPEAKER_{int(nearest['label']):02d}",
                "confidence": round(confidence, 3),
                "provider": TRANSCRIPT_EMBEDDING_BACKEND,
                "model": model_name,
                "metadata": {
                    "run_id": run_id,
                    "audio_path": str(audio_path),
                    "transcript_segment_id": row.get("id"),
                    "transcript_text": row.get("text"),
                    "anchor_transcript_segment_id": anchor_row.get("id"),
                    "anchor_distance_s": round(distance_s, 3),
                    "method": "speechbrain anchors clustered from transcript-aligned audio windows",
                },
            }
        )
    return labeled_rows


def _nearest_anchor(anchors: list[dict[str, Any]], anchor_midpoints: list[float], midpoint: float) -> dict[str, Any] | None:
    if not anchors:
        return None
    insertion = bisect_left(anchor_midpoints, midpoint)
    candidates = []
    if insertion < len(anchors):
        candidates.append(anchors[insertion])
    if insertion > 0:
        candidates.append(anchors[insertion - 1])
    return min(candidates, key=lambda item: abs(float(item["midpoint"]) - midpoint)) if candidates else None


def _encode_speechbrain_embeddings(classifier: Any, clips: list[Any], clip_lengths: list[int], *, torch: Any) -> Any:
    embeddings = []
    batch_size = 32
    with torch.no_grad():
        for index in range(0, len(clips), batch_size):
            batch_clips = clips[index : index + batch_size]
            batch_lengths = clip_lengths[index : index + batch_size]
            padded = torch.nn.utils.rnn.pad_sequence(batch_clips, batch_first=True)
            wav_lens = torch.tensor(
                [length / max(batch_lengths) for length in batch_lengths],
                dtype=padded.dtype,
                device=padded.device,
            )
            batch_embeddings = classifier.encode_batch(padded, wav_lens=wav_lens)
            batch_embeddings = batch_embeddings.detach().cpu().numpy()
            batch_embeddings = batch_embeddings.reshape(batch_embeddings.shape[0], -1)
            embeddings.extend(batch_embeddings)
    import numpy as np  # type: ignore

    return np.asarray(embeddings, dtype="float32")


def _cluster_voice_embeddings(embeddings: Any, *, distance_threshold: float | None = None) -> list[int]:
    if len(embeddings) == 0:
        return []
    if len(embeddings) == 1:
        return [0]
    if distance_threshold is None:
        distance_threshold = _speaker_cluster_distance_threshold()
    from sklearn.cluster import AgglomerativeClustering  # type: ignore

    try:
        clustering = AgglomerativeClustering(
            n_clusters=None,
            metric="cosine",
            linkage="average",
            distance_threshold=distance_threshold,
        )
    except TypeError:
        clustering = AgglomerativeClustering(
            n_clusters=None,
            affinity="cosine",
            linkage="average",
            distance_threshold=distance_threshold,
        )
    return [int(label) for label in clustering.fit_predict(embeddings)]


def _canonicalize_cluster_labels(labels: list[int]) -> list[int]:
    mapping: dict[int, int] = {}
    canonical = []
    for label in labels:
        if label not in mapping:
            mapping[label] = len(mapping)
        canonical.append(mapping[label])
    return canonical


def _cluster_confidences(embeddings: Any, labels: list[int]) -> list[float]:
    if len(labels) == 0:
        return []
    import numpy as np  # type: ignore

    matrix = np.asarray(embeddings, dtype="float32")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.clip(norms, 1e-12, None)
    centroids = {}
    for label in sorted(set(labels)):
        members = matrix[[index for index, item in enumerate(labels) if item == label]]
        centroid = members.mean(axis=0)
        centroid = centroid / max(float(np.linalg.norm(centroid)), 1e-12)
        centroids[label] = centroid
    confidences = []
    for embedding, label in zip(matrix, labels, strict=False):
        similarity = float(np.dot(embedding, centroids[label]))
        confidences.append(max(0.0, min(1.0, (similarity + 1.0) / 2.0)))
    return confidences


def _merge_adjacent_speaker_segments(segments: list[dict[str, Any]], *, max_gap_s: float = 1.5) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for segment in sorted(segments, key=lambda item: (str(item.get("source_video_id")), float(item.get("start_s") or 0.0))):
        if (
            merged
            and segment["source_video_id"] == merged[-1]["source_video_id"]
            and segment["speaker_label"] == merged[-1]["speaker_label"]
            and float(segment["start_s"]) - float(merged[-1]["end_s"]) <= max_gap_s
        ):
            previous = merged[-1]
            previous["end_s"] = round(max(float(previous["end_s"]), float(segment["end_s"])), 3)
            previous["confidence"] = round((float(previous["confidence"] or 0.0) + float(segment["confidence"] or 0.0)) / 2.0, 3)
            previous_metadata = previous.setdefault("metadata", {})
            transcript_ids = previous_metadata.setdefault("transcript_segment_ids", [])
            if previous_metadata.get("transcript_segment_id"):
                transcript_ids.append(previous_metadata.pop("transcript_segment_id"))
            if segment.get("metadata", {}).get("transcript_segment_id"):
                transcript_ids.append(segment["metadata"]["transcript_segment_id"])
            continue
        merged.append(dict(segment))
    return merged


def _safe_model_dir(model_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", model_name).strip("_") or "model"


def _speaker_embedding_cache_path(project: Path, source_video_id: str, model_name: str) -> Path:
    return project / ".cache" / "speaker_embeddings" / f"{source_video_id}_{_safe_model_dir(model_name)}.jsonl"


def _load_speaker_embedding_cache(path: Path, rows: list[dict[str, Any]], model_name: str) -> Any | None:
    if not path.exists() or not rows:
        return None
    import numpy as np  # type: ignore

    cached = {}
    for item in read_jsonl(path):
        if item.get("model") != model_name or not item.get("transcript_segment_id"):
            continue
        vector = item.get("embedding")
        if isinstance(vector, list):
            cached[str(item["transcript_segment_id"])] = vector
    ordered = []
    for row in rows:
        key = str(row.get("id") or "")
        vector = cached.get(key)
        if vector is None:
            return None
        ordered.append(vector)
    return np.asarray(ordered, dtype="float32")


def _write_speaker_embedding_cache(path: Path, rows: list[dict[str, Any]], embeddings: Any, model_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    for row, embedding in zip(rows, embeddings, strict=False):
        append_jsonl(
            path,
            {
                "source_video_id": row.get("source_video_id"),
                "transcript_segment_id": row.get("id"),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
                "model": model_name,
                "embedding": [round(float(value), 6) for value in embedding],
            },
        )


def _speaker_cluster_distance_threshold() -> float:
    value = os.environ.get("TAPESPLIT_SPEAKER_CLUSTER_DISTANCE")
    if not value:
        return TRANSCRIPT_EMBEDDING_CLUSTER_DISTANCE
    try:
        return float(value)
    except ValueError:
        return TRANSCRIPT_EMBEDDING_CLUSTER_DISTANCE


def _row_midpoint(row: dict[str, Any]) -> float:
    start = _number_or_none(row.get("start_s")) or 0.0
    end = _number_or_none(row.get("end_s"))
    if end is None or end < start:
        end = start
    return (start + end) / 2.0


def _row_duration(row: dict[str, Any]) -> float:
    start = _number_or_none(row.get("start_s")) or 0.0
    end = _number_or_none(row.get("end_s"))
    if end is None or end < start:
        return 0.0
    return end - start


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

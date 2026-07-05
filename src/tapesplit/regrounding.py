"""Re-ground mislocated events by searching the tape for their content.

Whole-video analyses of long tapes report drifted timestamps and sometimes
attach a real description to the wrong minutes ("tooth pulled at home" over
ski-forest footage). The archive already holds everything needed to fix this
locally: CLIP embeddings for every scene keyframe (text and image share the
space), BLIP captions, and the tape-absolute transcript.

For each suspect event this module scores every content scene on its tape
against the event's story (CLIP text→image similarity + caption/transcript
token overlap), finds the best contiguous window, and — when that window
clearly beats the currently claimed range — proposes moving the event there.
Proposals land in ``event_regroundings.jsonl``; the review queue turns them
into one-click (or safe-policy auto-accepted) ``move_event_range`` actions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tapesplit.storage import append_jsonl, read_jsonl

DEFAULT_MIN_SCORE = 0.24
DEFAULT_MIN_MARGIN = 0.05
DEFAULT_WINDOW_SCENES = 3

_TOKEN_PATTERN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def build_event_regroundings(
    project_dir: Path,
    *,
    only_unverified: bool = True,
    min_score: float = DEFAULT_MIN_SCORE,
    min_margin: float = DEFAULT_MIN_MARGIN,
    window_scenes: int = DEFAULT_WINDOW_SCENES,
    encode_text: Callable[[list[str]], list[list[float]]] | None = None,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    events = read_jsonl(project / "canonical_events.jsonl")
    if not events:
        raise FileNotFoundError(f"no canonical events in {project}")

    alignments_by_event = {
        str(row.get("canonical_event_id")): row
        for row in read_jsonl(project / "event_alignments.jsonl")
        if row.get("canonical_event_id")
    }
    candidates = []
    for event in events:
        if str(event.get("relatedness") or "").find("unrelated") >= 0:
            continue
        if only_unverified and not _alignment_unverified(alignments_by_event.get(str(event.get("id")))):
            continue
        source_range = _primary_source_range(event)
        if source_range:
            candidates.append((event, source_range))

    scene_vectors = _scene_vectors_by_source(project)
    captions_by_asset = {
        str(row.get("visual_asset_id") or row.get("source_subject_id") or ""): str(row.get("caption") or "")
        for row in read_jsonl(project / "visual_captions.jsonl")
    }
    transcripts_by_source: dict[str, list[dict[str, Any]]] = {}
    for segment in read_jsonl(project / "transcript_segments.jsonl"):
        source = str(segment.get("source_video_id") or "")
        if source:
            transcripts_by_source.setdefault(source, []).append(segment)

    if encode_text is None:
        encode_text = _default_text_encoder(project)

    queries = [
        f"{event.get('title') or ''}. {str(event.get('summary') or '')[:300]}"
        for event, _ in candidates
    ]
    query_vectors = encode_text(queries) if queries else []

    proposals = []
    skipped = {"no_scene_vectors": 0, "weak_match": 0, "already_grounded": 0}
    for (event, source_range), query_vector in zip(candidates, query_vectors, strict=False):
        source_video_id = source_range["source_video_id"]
        scenes = scene_vectors.get(source_video_id, [])
        if len(scenes) < window_scenes:
            skipped["no_scene_vectors"] += 1
            continue
        proposal = _propose_regrounding(
            event,
            source_range,
            query_vector,
            scenes,
            captions_by_asset,
            transcripts_by_source.get(source_video_id, []),
            min_score=min_score,
            min_margin=min_margin,
            window_scenes=window_scenes,
        )
        if proposal is None:
            skipped["weak_match"] += 1
            continue
        if proposal.get("already_grounded"):
            skipped["already_grounded"] += 1
            continue
        proposals.append(proposal)

    output = project / "event_regroundings.jsonl"
    if output.exists():
        output.unlink()
    for proposal in proposals:
        append_jsonl(output, proposal)

    return {
        "project": str(project),
        "candidates": len(candidates),
        "proposals": len(proposals),
        "skipped": skipped,
        "output": str(output),
    }


def _propose_regrounding(
    event: dict[str, Any],
    source_range: dict[str, Any],
    query_vector: list[float],
    scenes: list[dict[str, Any]],
    captions_by_asset: dict[str, str],
    transcripts: list[dict[str, Any]],
    *,
    min_score: float,
    min_margin: float,
    window_scenes: int,
) -> dict[str, Any] | None:
    query_tokens = {
        match.group(0).lower()
        for text in (str(event.get("title") or ""), str(event.get("summary") or ""))
        for match in _TOKEN_PATTERN.finditer(text)
    }

    scored = []
    for scene in scenes:
        visual = _cosine(query_vector, scene["vector"])
        caption = captions_by_asset.get(scene["asset_id"], "")
        caption_tokens = {match.group(0).lower() for match in _TOKEN_PATTERN.finditer(caption)}
        caption_overlap = len(caption_tokens & query_tokens)
        score = visual + min(caption_overlap, 3) * 0.02
        scored.append({**scene, "score": score})
    scored.sort(key=lambda row: row["time_s"])

    def window_score(index: int) -> float:
        window = scored[index : index + window_scenes]
        return sum(row["score"] for row in window) / len(window)

    best_index = max(range(len(scored) - window_scenes + 1), key=window_score)
    best = window_score(best_index)

    current_start = float(source_range.get("start_s") or 0.0)
    current_end = float(source_range.get("end_s") or current_start)
    current_rows = [row for row in scored if row["time_s"] >= current_start and row["time_s"] <= current_end]
    current = (
        sum(row["score"] for row in current_rows) / len(current_rows)
        if current_rows
        else min(row["score"] for row in scored)
    )

    window = scored[best_index : best_index + window_scenes]
    proposed_start = min(row["start_s"] for row in window)
    proposed_end = max(row["end_s"] for row in window)

    # Transcript corroboration: does the story's wording appear near the
    # proposed window?
    transcript_hit = False
    for segment in transcripts:
        seg_start = segment.get("start_s")
        if seg_start is None or seg_start < proposed_start - 60 or seg_start > proposed_end + 60:
            continue
        seg_tokens = {match.group(0).lower() for match in _TOKEN_PATTERN.finditer(str(segment.get("text") or ""))}
        if len(seg_tokens & query_tokens) >= 2:
            transcript_hit = True
            break

    overlaps_current = proposed_start <= current_end and proposed_end >= current_start
    if overlaps_current:
        return {"already_grounded": True}

    margin = best - current
    if best < min_score or margin < min_margin:
        return None

    confidence = round(min(0.92, 0.5 + margin * 3 + (0.1 if transcript_hit else 0.0)), 3)
    signals = [f"visual match {best:.3f} vs {current:.3f} at claimed range"]
    if transcript_hit:
        signals.append("transcript near proposed window matches story wording")

    return {
        "id": f"reground_{event.get('id')}",
        "canonical_event_id": str(event.get("id") or ""),
        "title": event.get("title"),
        "source_video_id": source_range["source_video_id"],
        "current_start_s": round(current_start, 3),
        "current_end_s": round(current_end, 3),
        "proposed_start_s": round(proposed_start, 3),
        "proposed_end_s": round(proposed_end, 3),
        "score": round(best, 4),
        "current_score": round(current, 4),
        "margin": round(margin, 4),
        "transcript_corroborated": transcript_hit,
        "confidence": confidence,
        "signals": signals,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _scene_vectors_by_source(project: Path) -> dict[str, list[dict[str, Any]]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(project / "visual_embeddings.jsonl"):
        if str(row.get("source_subject_type") or "") != "scene":
            continue
        vector = row.get("vector")
        source = str(row.get("source_video_id") or "")
        time_s = row.get("time_s")
        if not vector or not source or time_s is None:
            continue
        by_source.setdefault(source, []).append(
            {
                "vector": vector,
                "time_s": float(time_s),
                "start_s": float(row.get("start_s") or time_s),
                "end_s": float(row.get("end_s") or time_s),
                "asset_id": str(row.get("visual_asset_id") or row.get("id") or ""),
            }
        )
    return by_source


def _primary_source_range(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    for source in (event.get("source_ranges"), metadata.get("source_ranges")):
        if isinstance(source, list):
            for item in source:
                if isinstance(item, dict) and item.get("source_video_id") and item.get("start_s") is not None:
                    return {
                        "source_video_id": str(item["source_video_id"]),
                        "start_s": float(item["start_s"]),
                        "end_s": float(item.get("end_s") or item["start_s"]),
                    }
    return None


def _alignment_unverified(alignment: dict[str, Any] | None) -> bool:
    if not isinstance(alignment, dict):
        return True
    timing = str(alignment.get("timing_status") or "")
    if timing not in {"weakly_aligned", "unaligned", "unsupported", "model_only"}:
        return False
    support = alignment.get("support_score")
    try:
        return support is None or float(support) <= 0.45
    except (TypeError, ValueError):
        return True


def _default_text_encoder(project: Path) -> Callable[[list[str]], list[list[float]]]:
    from tapesplit.visual_embeddings import DEFAULT_VISUAL_EMBEDDING_MODEL

    rows = read_jsonl(project / "visual_embeddings.jsonl")
    model_name = DEFAULT_VISUAL_EMBEDDING_MODEL
    for row in rows:
        if row.get("embedding_model"):
            model_name = str(row["embedding_model"])
            break
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "event re-grounding needs sentence-transformers (install '.[visual-ai]')"
        ) from exc
    model = SentenceTransformer(model_name)

    def encode(texts: list[str]) -> list[list[float]]:
        return [list(map(float, vector)) for vector in model.encode(texts, show_progress_bar=False)]

    return encode


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)

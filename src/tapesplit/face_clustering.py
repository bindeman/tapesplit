from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


DEFAULT_FACE_CLUSTER_DISTANCE = 0.28


def cluster_faces_for_project(
    project_dir: Path,
    *,
    max_distance: float = DEFAULT_FACE_CLUSTER_DISTANCE,
    min_cluster_size: int = 1,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    if max_distance <= 0:
        raise ValueError("max_distance must be greater than 0")
    if min_cluster_size <= 0:
        raise ValueError("min_cluster_size must be greater than 0")

    faces = read_jsonl(project / "face_observations.jsonl")
    if not faces:
        raise FileNotFoundError("no face observations found; run `tapesplit detect-faces` first")

    visibility = build_visibility_filter(project)
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {str(row.get("id")): row for row in evidence_rows if row.get("id")}
    events = [
        event
        for event in read_jsonl(project / "canonical_events.jsonl")
        if visibility.visible_row(event, evidence_by_id=evidence_by_id)
    ]
    people = [
        person
        for person in read_jsonl(project / "people_groups.jsonl")
        if visibility.visible_row(person, evidence_by_id=evidence_by_id)
    ]

    feature_rows = []
    skipped = 0
    for face in sorted(faces, key=_face_sort_key):
        try:
            feature = _face_feature(project, face)
        except RuntimeError:
            raise
        except Exception:
            feature = []
        if not feature:
            skipped += 1
            continue
        feature_rows.append({"face": face, "feature": feature})

    clusters = _cluster_feature_rows(feature_rows, max_distance=max_distance)
    clusters = [cluster for cluster in clusters if len(cluster["faces"]) >= min_cluster_size]
    face_to_cluster_id = {
        str(face.get("id")): f"face_cluster_{index:06d}"
        for index, cluster in enumerate(clusters, start=1)
        for face in cluster["faces"]
        if face.get("id")
    }
    enriched_faces = [
        {
            **face,
            "face_cluster_id": face_to_cluster_id.get(str(face.get("id") or ""), ""),
        }
        for face in faces
    ]
    _write_jsonl(project / "face_observations.jsonl", enriched_faces)

    context = _face_event_context(enriched_faces, events)
    people_by_event = _people_by_event(people)
    cluster_records = []
    candidate_records = []
    candidate_index = 1
    for index, cluster in enumerate(clusters, start=1):
        cluster_id = f"face_cluster_{index:06d}"
        faces_for_cluster = cluster["faces"]
        candidates = _candidate_people_for_cluster(
            faces_for_cluster,
            face_event_context=context,
            people_by_event=people_by_event,
        )
        cluster_record = _cluster_record(
            cluster_id,
            faces_for_cluster,
            max_distance=max_distance,
            candidate_people=candidates,
        )
        cluster_records.append(cluster_record)
        for candidate in candidates:
            candidate_records.append(
                {
                    "id": f"face_identity_candidate_{candidate_index:06d}",
                    "face_cluster_id": cluster_id,
                    **candidate,
                    "review_status": "needs_review",
                    "source": "local_face_cluster_event_context",
                }
            )
            candidate_index += 1

    _write_jsonl(project / "face_clusters.jsonl", cluster_records)
    _write_jsonl(project / "face_identity_candidates.jsonl", candidate_records)
    return {
        "project": str(project),
        "face_observations": len(faces),
        "faces_clustered": len(face_to_cluster_id),
        "faces_skipped": skipped,
        "face_clusters": len(cluster_records),
        "identity_candidates": len(candidate_records),
        "max_distance": max_distance,
        "min_cluster_size": min_cluster_size,
        "outputs": {
            "face_observations": str(project / "face_observations.jsonl"),
            "face_clusters": str(project / "face_clusters.jsonl"),
            "face_identity_candidates": str(project / "face_identity_candidates.jsonl"),
        },
    }


def _face_feature(project: Path, face: dict[str, Any]) -> list[float]:
    cv2 = _load_cv2()
    image_path = project / str(face.get("face_thumbnail_path") or "")
    image = cv2.imread(str(image_path))
    if image is None:
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (32, 32))
    equalized = cv2.equalizeHist(resized)
    values = [float(value) / 255.0 for row in equalized.tolist() for value in row]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _cluster_feature_rows(rows: list[dict[str, Any]], *, max_distance: float) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for row in rows:
        best_cluster = None
        best_distance = None
        for cluster in clusters:
            distance = _cosine_distance(row["feature"], cluster["centroid"])
            if best_distance is None or distance < best_distance:
                best_cluster = cluster
                best_distance = distance
        if best_cluster is not None and best_distance is not None and best_distance <= max_distance:
            best_cluster["faces"].append(row["face"])
            best_cluster["features"].append(row["feature"])
            best_cluster["centroid"] = _centroid(best_cluster["features"])
            best_cluster["max_observed_distance"] = max(best_cluster["max_observed_distance"], best_distance)
        else:
            clusters.append(
                {
                    "faces": [row["face"]],
                    "features": [row["feature"]],
                    "centroid": row["feature"],
                    "max_observed_distance": 0.0,
                }
            )
    return clusters


def _cluster_record(
    cluster_id: str,
    faces: list[dict[str, Any]],
    *,
    max_distance: float,
    candidate_people: list[dict[str, Any]],
) -> dict[str, Any]:
    source_video_ids = _unique_items(str(face.get("source_video_id") or "") for face in faces)
    face_ids = [str(face.get("id")) for face in faces if face.get("id")]
    starts = [_number_or_none(face.get("start_s") or face.get("time_s")) for face in faces]
    ends = [_number_or_none(face.get("end_s") or face.get("time_s")) for face in faces]
    first_start = min([value for value in starts if value is not None], default=None)
    last_end = max([value for value in ends if value is not None], default=None)
    representative = faces[0] if faces else {}
    review_status = "needs_review" if candidate_people or len(faces) > 1 else "unreviewed"
    return {
        "id": cluster_id,
        "label": f"Face cluster {int(cluster_id.rsplit('_', 1)[-1])}",
        "face_observation_ids": face_ids,
        "face_count": len(face_ids),
        "source_video_ids": source_video_ids,
        "source_subject_ids": _unique_items(str(face.get("source_subject_id") or "") for face in faces),
        "first_start_s": first_start,
        "last_end_s": last_end,
        "representative_face_observation_id": str(representative.get("id") or ""),
        "thumbnail_path": str(representative.get("face_thumbnail_path") or ""),
        "candidate_people": candidate_people,
        "linked_person_group_id": "",
        "review_status": review_status,
        "method": "local_face_thumbnail_similarity",
        "feature_model": "opencv_equalized_gray_32",
        "max_distance": max_distance,
        "notes": [
            "Face clusters are visual similarity candidates, not confirmed identities.",
            "Candidate people are inferred from event co-occurrence and require review.",
        ],
    }


def _candidate_people_for_cluster(
    faces: list[dict[str, Any]],
    *,
    face_event_context: dict[str, list[dict[str, Any]]],
    people_by_event: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for face in faces:
        face_id = str(face.get("id") or "")
        for event in face_event_context.get(face_id, []):
            event_id = str(event.get("id") or "")
            for person in people_by_event.get(event_id, []):
                person_id = str(person.get("id") or "")
                if not person_id:
                    continue
                bucket = buckets.setdefault(
                    person_id,
                    {
                        "person_group_id": person_id,
                        "person_label": str(person.get("label") or ""),
                        "face_observation_ids": [],
                        "supporting_event_ids": [],
                        "supporting_event_titles": [],
                    },
                )
                if face_id and face_id not in bucket["face_observation_ids"]:
                    bucket["face_observation_ids"].append(face_id)
                if event_id and event_id not in bucket["supporting_event_ids"]:
                    bucket["supporting_event_ids"].append(event_id)
                    bucket["supporting_event_titles"].append(str(event.get("title") or event_id))
    candidates = []
    face_count = max(1, len(faces))
    for bucket in buckets.values():
        face_support = len(bucket["face_observation_ids"])
        event_support = len(bucket["supporting_event_ids"])
        confidence = min(0.85, 0.25 + (face_support / face_count) * 0.35 + min(event_support * 0.08, 0.25))
        candidates.append(
            {
                **bucket,
                "confidence": round(confidence, 3),
                "basis": [
                    "candidate person appears in events overlapping this face cluster",
                    "not a confirmed identity",
                ],
            }
        )
    candidates.sort(key=lambda row: (row["confidence"], len(row["supporting_event_ids"]), row["person_label"]), reverse=True)
    return candidates[:5]


def _face_event_context(faces: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    intervals = []
    for event in events:
        for interval in _event_intervals(event):
            intervals.append({"event": event, **interval})
    context: dict[str, list[dict[str, Any]]] = {}
    for face in faces:
        face_id = str(face.get("id") or "")
        if not face_id:
            continue
        if face.get("source_subject_type") == "event" and face.get("source_subject_id"):
            event = next((item for item in events if str(item.get("id") or "") == str(face["source_subject_id"])), None)
            context[face_id] = [event] if event else []
            continue
        source_video_id = str(face.get("source_video_id") or "")
        time_s = _number_or_none(face.get("time_s") or face.get("start_s"))
        if not source_video_id or time_s is None:
            context[face_id] = []
            continue
        matches = [
            row["event"]
            for row in intervals
            if row["source_video_id"] == source_video_id
            and row["start_s"] <= time_s <= row["end_s"]
        ]
        context[face_id] = matches
    return context


def _event_intervals(event: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    intervals = []
    for source in [event.get("source_ranges"), metadata.get("source_ranges")]:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            interval = _interval_from_values(item.get("source_video_id"), item.get("start_s"), item.get("end_s"))
            if interval:
                intervals.append(interval)
    source_video_id = event.get("source_video_id") or metadata.get("source_video_id")
    interval = _interval_from_values(source_video_id, event.get("start_s"), event.get("end_s"))
    if interval:
        intervals.append(interval)
    return _unique_intervals(intervals)


def _interval_from_values(source_video_id: Any, start_s: Any, end_s: Any) -> dict[str, Any] | None:
    source = str(source_video_id or "")
    start = _number_or_none(start_s)
    end = _number_or_none(end_s)
    if not source or start is None:
        return None
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    return {"source_video_id": source, "start_s": start, "end_s": end}


def _people_by_event(people: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for person in people:
        for event_id in person.get("canonical_event_ids") or []:
            grouped[str(event_id)].append(person)
    return dict(grouped)


def _centroid(features: list[list[float]]) -> list[float]:
    if not features:
        return []
    dims = len(features[0])
    values = [sum(feature[index] for feature in features) / len(features) for index in range(dims)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _cosine_distance(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    return max(0.0, min(2.0, 1.0 - dot))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)
    if not rows:
        path.write_text("", encoding="utf-8")


def _face_sort_key(face: dict[str, Any]) -> tuple[str, float, str]:
    return (
        str(face.get("source_video_id") or ""),
        _number_or_large(face.get("time_s") or face.get("start_s")),
        str(face.get("id") or ""),
    )


def _unique_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for interval in intervals:
        key = (interval["source_video_id"], interval["start_s"], interval["end_s"])
        if key in seen:
            continue
        seen.add(key)
        result.append(interval)
    return result


def _unique_items(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "")
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_or_large(value: Any) -> float:
    number = _number_or_none(value)
    return number if number is not None else 1_000_000_000.0


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for local face clustering. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc
    return cv2

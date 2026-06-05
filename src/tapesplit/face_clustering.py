from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
import re
from typing import Any

from tapesplit.face_quality import analyze_face_quality
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


DEFAULT_FACE_CLUSTER_DISTANCE = 0.28

FACE_IDENTITY_EXCLUDED_KINDS = {
    "role_candidate",
    "fictional_person",
    "group_candidate",
}

FACE_IDENTITY_GROUP_LABELS = {
    "adult",
    "adults",
    "children",
    "family",
    "friends",
    "group",
    "kids",
    "people",
    "russians",
}


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

    prepared_faces = []
    feature_rows = []
    skipped = 0
    skipped_low_quality = 0
    review_only_faces = []
    for face in sorted(faces, key=_face_sort_key):
        quality = analyze_face_quality(project / str(face.get("face_thumbnail_path") or ""))
        prepared_face = {
            **face,
            "face_quality": quality,
            "face_quality_status": quality.get("status"),
            "face_quality_notes": quality.get("notes") or [],
        }
        prepared_faces.append(prepared_face)
        if not quality.get("usable"):
            skipped_low_quality += 1
            review_only_faces.append(prepared_face)
            continue
        try:
            feature = _face_feature(project, prepared_face)
        except RuntimeError:
            raise
        except Exception:
            feature = []
        if not feature:
            skipped += 1
            continue
        feature_rows.append({"face": prepared_face, "feature": feature})

    clusters = _cluster_feature_rows(feature_rows, max_distance=max_distance)
    clusters = [cluster for cluster in clusters if len(cluster["faces"]) >= min_cluster_size]
    if min_cluster_size <= 1:
        clusters.extend(_review_only_face_clusters(review_only_faces))
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
        for face in prepared_faces
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
        "faces_skipped_from_similarity": skipped + skipped_low_quality,
        "faces_skipped_low_quality": skipped_low_quality,
        "faces_review_only_low_quality": len(review_only_faces) if min_cluster_size <= 1 else 0,
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


def _review_only_face_clusters(faces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "faces": [face],
            "features": [],
            "centroid": [],
            "max_observed_distance": None,
            "review_only": True,
        }
        for face in faces
    ]


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
    quality = _cluster_quality(faces)
    review_only = bool(faces) and not any(face.get("face_quality_status") == "usable" for face in faces)
    notes = [
        "Face clusters are visual similarity candidates, not confirmed identities.",
        "Candidate people are inferred from event co-occurrence and require review.",
    ]
    if review_only:
        review_status = "needs_review"
        notes.append("This is a review-only low-quality face crop; it was not used for visual similarity matching.")
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
        "review_only": review_only,
        "quality_status": quality["status"],
        "face_quality_counts": quality["counts"],
        "face_quality_notes": quality["notes"],
        "low_quality_face_count": quality["low_quality_count"],
        "notes": notes,
    }


def _candidate_people_for_cluster(
    faces: list[dict[str, Any]],
    *,
    face_event_context: dict[str, list[dict[str, Any]]],
    people_by_event: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    cluster_event_ids: set[str] = set()
    for face in faces:
        face_id = str(face.get("id") or "")
        for event in face_event_context.get(face_id, []):
            event_id = str(event.get("id") or "")
            if event_id:
                cluster_event_ids.add(event_id)
            event_people = [person for person in people_by_event.get(event_id, []) if _identity_eligible_person(person)]
            event_people_count = max(1, len(event_people))
            event_weight = 1.0 / math.sqrt(event_people_count)
            for person in event_people:
                person_id = str(person.get("id") or "")
                if not person_id:
                    continue
                direct_name_signal = _person_event_name_signal(person, event)
                bucket = buckets.setdefault(
                    person_id,
                    {
                        "person_group_id": person_id,
                        "person_label": str(person.get("label") or ""),
                        "face_observation_ids": [],
                        "supporting_event_ids": [],
                        "supporting_event_titles": [],
                        "direct_name_event_ids": [],
                        "direct_name_strength": 0.0,
                        "event_person_counts": {},
                        "_score": 0.0,
                        "_direct_name_strength": 0.0,
                    },
                )
                if face_id and face_id not in bucket["face_observation_ids"]:
                    bucket["face_observation_ids"].append(face_id)
                if event_id and event_id not in bucket["supporting_event_ids"]:
                    bucket["supporting_event_ids"].append(event_id)
                    bucket["supporting_event_titles"].append(str(event.get("title") or event_id))
                if event_id:
                    bucket["event_person_counts"][event_id] = event_people_count
                    if direct_name_signal and event_id not in bucket["direct_name_event_ids"]:
                        bucket["direct_name_event_ids"].append(event_id)
                bucket["_score"] += event_weight + direct_name_signal
                bucket["_direct_name_strength"] += direct_name_signal
                bucket["direct_name_strength"] = round(bucket["_direct_name_strength"], 3)
    candidates = []
    face_count = max(1, len(faces))
    cluster_event_count = max(1, len(cluster_event_ids))
    quality = _cluster_quality(faces)
    quality_multiplier = _quality_confidence_multiplier(quality)
    for bucket in buckets.values():
        face_support = len(bucket["face_observation_ids"])
        event_support = len(bucket["supporting_event_ids"])
        direct_support = len(bucket["direct_name_event_ids"])
        coverage = face_support / face_count
        event_coverage = event_support / cluster_event_count
        direct_rate = direct_support / max(1, event_support)
        direct_strength_rate = min(float(bucket["_direct_name_strength"]) / max(1, event_support), 1.0)
        avg_people_count = (
            sum(bucket["event_person_counts"].values()) / len(bucket["event_person_counts"])
            if bucket["event_person_counts"]
            else 1.0
        )
        confidence = (
            0.12
            + coverage * 0.28
            + min(event_coverage, 1.0) * 0.18
            + min(bucket["_score"] / face_count, 1.0) * 0.18
            + direct_rate * 0.15
            + direct_strength_rate * 0.07
        )
        if direct_support == 0:
            confidence = min(confidence, 0.56)
        if avg_people_count >= 4 and direct_support == 0:
            confidence -= 0.08
        confidence *= quality_multiplier
        if quality["status"] != "usable":
            confidence = min(confidence, 0.52 if direct_support else 0.38)
        confidence = max(0.05, min(0.9, confidence))
        ambiguity = "low" if avg_people_count <= 1.5 else "medium" if avg_people_count <= 3 else "high"
        basis = [
            "candidate person appears in events overlapping this face cluster",
            *(
                ["event title/summary directly names this person"]
                if direct_support
                else ["no direct visual identity signal; event co-occurrence only"]
            ),
        ]
        if quality["status"] != "usable":
            basis.append("face crop quality is weak; use as review evidence only")
        basis.append("not a confirmed identity")
        candidates.append(
            {
                **{key: value for key, value in bucket.items() if not key.startswith("_")},
                "confidence": round(confidence, 3),
                "candidate_ambiguity": ambiguity,
                "average_event_people_count": round(avg_people_count, 2),
                "face_quality_status": quality["status"],
                "face_quality_notes": quality["notes"],
                "basis": basis,
            }
        )
    candidates.sort(
        key=lambda row: (
            row["confidence"],
            row.get("direct_name_strength") or 0.0,
            len(row.get("direct_name_event_ids") or []),
            len(row["supporting_event_ids"]),
            row["person_label"],
        ),
        reverse=True,
    )
    return candidates[:5]


def _cluster_quality(faces: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(face.get("face_quality_status") or "unknown") for face in faces)
    low_quality_count = sum(count for status, count in counts.items() if status not in {"usable", "unknown"})
    notes = _unique_items(
        note
        for face in faces
        for note in face.get("face_quality_notes") or []
    )
    if not faces:
        status = "unknown"
    elif counts.get("usable") == len(faces):
        status = "usable"
    elif counts.get("usable"):
        status = "mixed_quality"
    else:
        status = "low_quality"
    return {
        "status": status,
        "counts": dict(sorted(counts.items())),
        "low_quality_count": low_quality_count,
        "notes": notes,
    }


def _quality_confidence_multiplier(quality: dict[str, Any]) -> float:
    status = str(quality.get("status") or "")
    if status == "usable":
        return 1.0
    if status == "mixed_quality":
        return 0.82
    if status == "low_quality":
        return 0.62
    return 0.7


def _identity_eligible_person(person: dict[str, Any]) -> bool:
    kind = str(person.get("kind") or "person_candidate")
    if kind in FACE_IDENTITY_EXCLUDED_KINDS:
        return False
    labels = [str(person.get("label") or ""), *[str(alias) for alias in person.get("aliases") or []]]
    normalized = {_normalize_label(label) for label in labels if label}
    if normalized & FACE_IDENTITY_GROUP_LABELS:
        return False
    return True


def _person_event_name_signal(person: dict[str, Any], event: dict[str, Any]) -> float:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    weighted_sources = [
        (event.get("title"), 1.0),
        (metadata.get("title"), 1.0),
        (event.get("summary"), 0.65),
        (metadata.get("summary"), 0.65),
    ]
    return max(
        (weight for value, weight in weighted_sources if _person_terms_in_text(person, value)),
        default=0.0,
    )


def _person_terms_in_text(person: dict[str, Any], value: Any) -> bool:
    normalized_text = _normalize_label(value)
    if not normalized_text:
        return False
    tokens = set(normalized_text.split())
    for term in _person_name_terms(person):
        if " " in term:
            if f" {term} " in f" {normalized_text} ":
                return True
        elif term in tokens:
            return True
    return False


def _person_name_terms(person: dict[str, Any]) -> set[str]:
    terms = set()
    for label in [person.get("label"), *(person.get("aliases") or [])]:
        normalized = _normalize_label(label)
        if not normalized:
            continue
        if "/" in str(label):
            continue
        if normalized not in FACE_IDENTITY_GROUP_LABELS:
            terms.add(normalized)
        for part in normalized.split():
            if len(part) > 2 and part not in FACE_IDENTITY_GROUP_LABELS:
                terms.add(part)
    return terms


def _normalize_label(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


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

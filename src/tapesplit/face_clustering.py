from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
import io
import math
import platform
from pathlib import Path
import re
from typing import Any
import warnings

from tapesplit.face_embeddings import anchor_key, ensure_face_embeddings
from tapesplit.face_quality import analyze_face_quality
from tapesplit.storage import append_jsonl, read_jsonl
from tapesplit.visibility import build_visibility_filter


DEFAULT_FACE_CLUSTER_DISTANCE = 0.28
# Constrained average-linkage sweep on the family haul (buffalo_l, 1,125
# embedded faces; "blocked" = same-frame merges prevented by cannot-links,
# "integrity" = human-linked clusters staying whole):
#   thr   clusters  singletons  largest  blocked  integrity
#   0.55     653       425        31        5       0.91
#   0.60     549       340        36        7       0.91
#   0.65     441       242        60       12       1.00
#   0.70     353       166       120       20       1.00
#   0.75     280       113       186       33       1.00
# 0.65 is the sweet spot: full human-cluster integrity before the largest
# cluster starts doubling (120@0.70, 186@0.75 — the chaining regime the
# research audit measured as impure). Revisit 0.70 after face tracks (P1)
# tighten same-person distances.
DEFAULT_ARCFACE_FACE_CLUSTER_DISTANCE = 0.65
# KP-RPE cosine scale differs from buffalo_l; recalibrate with the closed-loop
# sweep once weights are cached locally. Placeholder until measured.
DEFAULT_CVLFACE_FACE_CLUSTER_DISTANCE = 0.7
DEFAULT_FACE_EMBEDDING_BACKEND = "auto"
FACE_EMBEDDING_BACKENDS = {"auto", "opencv-gray", "arcface-insightface", "cvlface-kprpe"}

# Faces below this quality weight may JOIN a cluster but never SEED one: a
# blurry 40px crop can inherit an identity from clean crops it matches, but it
# cannot found a cluster other faces get pulled into.
FACE_CLUSTER_SEED_MIN_WEIGHT = 0.18

# Cluster-id succession across re-cluster runs: a new cluster inherits a
# previous cluster's id when their member-anchor sets overlap at or above this
# Jaccard. Human-labeled clusters that fail the floor become review items.
SUCCESSION_MIN_JACCARD = 0.5

CVLFACE_MODEL_REPO = "minchul/cvlface_adaface_vit_base_kprpe_webface12m"
CVLFACE_ALIGNER_REPO = "minchul/cvlface_DFA_mobilenet"
CVLFACE_DOWNLOAD_ENV = "TAPESPLIT_FACE_ALLOW_DOWNLOAD"

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


def check_face_embedding_config() -> dict[str, Any]:
    resolved = _resolve_face_embedding_backend("auto")
    return {
        "face_embedding_arcface": _arcface_available(),
        "face_embedding_cvlface": _cvlface_available(),
        "face_embedding_default_backend": resolved,
        "face_embedding_default_max_distance": _default_face_cluster_distance(resolved),
    }


def cluster_faces_for_project(
    project_dir: Path,
    *,
    max_distance: float | None = None,
    min_cluster_size: int = 1,
    embedding_backend: str = DEFAULT_FACE_EMBEDDING_BACKEND,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    resolved_embedding_backend = _resolve_face_embedding_backend(embedding_backend)
    resolved_max_distance = (
        max_distance
        if max_distance is not None
        else _default_face_cluster_distance(resolved_embedding_backend)
    )
    if resolved_max_distance <= 0:
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

    # Previous run's clusters/observations, read before anything is
    # overwritten: cluster ids survive re-clustering via anchor succession.
    previous_clusters = read_jsonl(project / "face_clusters.jsonl")
    previous_observations = {
        str(row.get("id") or ""): row for row in read_jsonl(project / "face_observations.jsonl")
    }

    prepared_faces = []
    embedder = _create_face_embedder(resolved_embedding_backend)
    for face in sorted(faces, key=_face_sort_key):
        quality = analyze_face_quality(project / str(face.get("face_thumbnail_path") or ""))
        prepared_faces.append(
            {
                **face,
                "face_quality": quality,
                "face_quality_status": quality.get("status"),
                "face_quality_notes": quality.get("notes") or [],
                "face_quality_weight": quality.get("quality_weight", 1.0 if quality.get("usable") else 0.0),
            }
        )

    # One embedding pass, persisted: quality no longer excludes anyone. The
    # old Haar-eye gate manufactured 508 singleton "review-only" clusters on
    # the reference haul (71% of all clusters); now every crop the embedder
    # can read participates, weighted by quality.
    embedding_result = ensure_face_embeddings(
        project,
        prepared_faces,
        feature_model=embedder.feature_model,
        describe=lambda face: embedder.describe(project, face),
    )
    embedding_records = embedding_result["records"]

    feature_rows = []
    skipped = 0
    for prepared_face in prepared_faces:
        record = embedding_records.get(str(prepared_face.get("id") or "")) or {}
        vector = record.get("vector")
        if not vector:
            skipped += 1
            continue
        det_score = _number_or_none(record.get("det_score"))
        weight = float(prepared_face.get("face_quality_weight") or 0.0)
        if det_score is not None:
            weight *= max(0.2, min(1.0, det_score / 0.72))
        feature_rows.append(
            {
                "face": prepared_face,
                "feature": [float(value) for value in vector],
                "weight": round(max(0.01, weight), 4),
                "frame_key": _frame_key(prepared_face),
                "seed": weight >= FACE_CLUSTER_SEED_MIN_WEIGHT,
            }
        )

    hac = _cluster_feature_rows(feature_rows, max_distance=resolved_max_distance)
    clusters = [cluster for cluster in hac["clusters"] if len(cluster["faces"]) >= min_cluster_size]

    succession = _assign_cluster_ids(
        clusters,
        previous_clusters=previous_clusters,
        previous_observations=previous_observations,
    )
    face_to_cluster_id = {
        str(face.get("id")): cluster["cluster_id"]
        for cluster in clusters
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
    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        faces_for_cluster = cluster["faces"]
        candidates = _candidate_people_for_cluster(
            faces_for_cluster,
            face_event_context=context,
            people_by_event=people_by_event,
        )
        cluster_record = _cluster_record(
            cluster_id,
            faces_for_cluster,
            max_distance=resolved_max_distance,
            candidate_people=candidates,
            method=embedder.method,
            feature_model=embedder.feature_model,
        )
        inherited = cluster.get("inherited_fields") or {}
        for key, value in inherited.items():
            if key == "notes":
                for note in value:
                    if note not in cluster_record["notes"]:
                        cluster_record["notes"].append(note)
            elif value not in (None, "", []):
                cluster_record[key] = value
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
        "faces_unclustered": len(prepared_faces) - len(face_to_cluster_id),
        "faces_joined_without_seeding": hac["joined_without_seeding"],
        "cannot_link_merges_blocked": hac["cannot_link_merges_blocked"],
        "face_clusters": len(cluster_records),
        "clusters_inherited": succession["inherited"],
        "clusters_new": succession["created"],
        "succession_conflicts": succession["conflicts"],
        "identity_candidates": len(candidate_records),
        "embeddings": embedding_result["summary"],
        "requested_embedding_backend": _normalize_face_embedding_backend(embedding_backend),
        "resolved_embedding_backend": resolved_embedding_backend,
        "feature_model": embedder.feature_model,
        "max_distance": resolved_max_distance,
        "min_cluster_size": min_cluster_size,
        "outputs": {
            "face_observations": str(project / "face_observations.jsonl"),
            "face_clusters": str(project / "face_clusters.jsonl"),
            "face_identity_candidates": str(project / "face_identity_candidates.jsonl"),
            "face_embeddings": str(project / "face_embeddings.jsonl"),
        },
    }


class _OpenCVGrayEmbedder:
    method = "local_face_thumbnail_similarity"
    feature_model = "opencv_equalized_gray_32"

    def feature(self, project: Path, face: dict[str, Any]) -> list[float]:
        return _face_feature(project, face)

    def describe(self, project: Path, face: dict[str, Any]) -> dict[str, Any] | None:
        vector = self.feature(project, face)
        return {"vector": vector} if vector else None


class _ArcFaceInsightFaceEmbedder:
    method = "local_face_embedding_similarity"
    feature_model = "insightface_buffalo_l_arcface_512"

    def __init__(self) -> None:
        self._app: Any | None = None
        self._providers = _onnxruntime_providers()

    def feature(self, project: Path, face: dict[str, Any]) -> list[float]:
        described = self.describe(project, face)
        return described["vector"] if described else []

    def describe(self, project: Path, face: dict[str, Any]) -> dict[str, Any] | None:
        """One pass over the crop: embedding plus the free buffalo_l attributes.

        Age, gender, pose, and det_score ride along with the recognition
        embedding at zero extra model cost; they persist into
        face_embeddings.jsonl for quality weighting and the age dimension.
        """

        cv2 = _load_cv2()
        image_path = project / str(face.get("face_thumbnail_path") or "")
        image = cv2.imread(str(image_path))
        if image is None:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            observations = self._face_app().get(image)
        if not observations:
            return None
        observation = max(observations, key=_insightface_observation_score)
        embedding = getattr(observation, "normed_embedding", None)
        if embedding is None:
            embedding = getattr(observation, "embedding", None)
        if embedding is None:
            return None
        pose = getattr(observation, "pose", None)
        pose_values = list(pose) if pose is not None else []
        return {
            "vector": _normalized_vector(embedding),
            "det_score": float(getattr(observation, "det_score", 0.0) or 0.0),
            "age_raw": getattr(observation, "age", None),
            "gender_raw": str(getattr(observation, "sex", "") or ""),
            "pose": {
                "pitch": round(float(pose_values[0]), 2) if len(pose_values) > 0 else None,
                "yaw": round(float(pose_values[1]), 2) if len(pose_values) > 1 else None,
                "roll": round(float(pose_values[2]), 2) if len(pose_values) > 2 else None,
            },
        }

    def _face_app(self) -> Any:
        if self._app is not None:
            return self._app
        try:
            from insightface.app import FaceAnalysis  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "InsightFace is required for ArcFace face embeddings. Install with "
                "`python -m pip install -e '.[face-ai]'`."
            ) from exc
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore", FutureWarning)
            app = FaceAnalysis(name="buffalo_l", providers=self._providers)
            app.prepare(ctx_id=-1, det_size=(320, 320))
        self._app = app
        return app


class _CVLFaceKPRPEEmbedder:
    """CVLFace AdaFace ViT-B KP-RPE: low-resolution-robust upgrade over buffalo_l.

    TinyFace Rank-1 76.1 vs ~72.3 for AdaFace IR101 (and buffalo_l well below
    both) — exactly the 40-100px regime this archive's crops live in. Weights
    load from the local HF cache only unless TAPESPLIT_FACE_ALLOW_DOWNLOAD=1
    (the pair is ~1.8GB). buffalo_l remains the default/fallback backend.
    """

    method = "local_face_embedding_similarity"
    feature_model = "cvlface_adaface_vit_base_kprpe_webface12m_512"

    def __init__(self) -> None:
        self._model: Any | None = None
        self._aligner: Any | None = None
        self._torch: Any | None = None

    def feature(self, project: Path, face: dict[str, Any]) -> list[float]:
        described = self.describe(project, face)
        return described["vector"] if described else []

    def describe(self, project: Path, face: dict[str, Any]) -> dict[str, Any] | None:
        cv2 = _load_cv2()
        image_path = project / str(face.get("face_thumbnail_path") or "")
        image = cv2.imread(str(image_path))
        if image is None:
            return None
        torch, model, aligner = self._load()
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        tensor = torch.from_numpy(rgb).permute(2, 0, 1).float().unsqueeze(0)
        tensor = tensor / 127.5 - 1.0
        with torch.no_grad():
            # CVLFace wrapper API: the DFA aligner returns the aligned face
            # plus landmarks; KP-RPE recognition consumes both.
            aligned_x, _orig_ldmks, aligned_ldmks, score, _thetas, _bbox = aligner(tensor)
            embedding = model(aligned_x, aligned_ldmks)
        vector = embedding.squeeze(0).detach().cpu().tolist()
        if not vector:
            return None
        alignment_score = float(score.squeeze().item()) if hasattr(score, "squeeze") else None
        return {
            "vector": _normalized_vector(vector),
            "det_score": alignment_score,
        }

    def _load(self) -> tuple[Any, Any, Any]:
        if self._model is not None and self._aligner is not None:
            return self._torch, self._model, self._aligner
        if not _cvlface_available():
            raise RuntimeError(
                "CVLFace KP-RPE weights are not in the local Hugging Face cache. "
                f"Set {CVLFACE_DOWNLOAD_ENV}=1 to allow the ~1.8GB download, or use "
                "the arcface-insightface backend."
            )
        try:
            import torch  # type: ignore
            from transformers import AutoModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "transformers and torch are required for the cvlface-kprpe backend."
            ) from exc
        local_only = not _truthy_env(CVLFACE_DOWNLOAD_ENV)
        model = AutoModel.from_pretrained(
            CVLFACE_MODEL_REPO, trust_remote_code=True, local_files_only=local_only
        )
        aligner = AutoModel.from_pretrained(
            CVLFACE_ALIGNER_REPO, trust_remote_code=True, local_files_only=local_only
        )
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = model.to(device).eval()
        aligner = aligner.to(device).eval()
        self._torch, self._model, self._aligner = torch, model, aligner
        return torch, model, aligner


def _create_face_embedder(embedding_backend: str) -> Any:
    if embedding_backend == "arcface-insightface":
        return _ArcFaceInsightFaceEmbedder()
    if embedding_backend == "cvlface-kprpe":
        return _CVLFaceKPRPEEmbedder()
    if embedding_backend == "opencv-gray":
        return _OpenCVGrayEmbedder()
    raise ValueError(f"unsupported face embedding backend: {embedding_backend}")


def _normalize_face_embedding_backend(value: str) -> str:
    backend = value.strip().casefold().replace("_", "-")
    aliases = {
        "arcface": "arcface-insightface",
        "insightface": "arcface-insightface",
        "buffalo-l": "arcface-insightface",
        "opencv": "opencv-gray",
        "opencv-equalized-gray-32": "opencv-gray",
        "cvlface": "cvlface-kprpe",
        "kprpe": "cvlface-kprpe",
    }
    backend = aliases.get(backend, backend)
    if backend not in FACE_EMBEDDING_BACKENDS:
        raise ValueError(
            "face embedding backend must be auto, opencv-gray, arcface-insightface, or cvlface-kprpe"
        )
    return backend


def _resolve_face_embedding_backend(value: str) -> str:
    backend = _normalize_face_embedding_backend(value)
    if backend != "auto":
        return backend
    return "arcface-insightface" if _arcface_available() else "opencv-gray"


def _default_face_cluster_distance(embedding_backend: str) -> float:
    if embedding_backend == "arcface-insightface":
        return DEFAULT_ARCFACE_FACE_CLUSTER_DISTANCE
    if embedding_backend == "cvlface-kprpe":
        return DEFAULT_CVLFACE_FACE_CLUSTER_DISTANCE
    return DEFAULT_FACE_CLUSTER_DISTANCE


def _cvlface_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    if _truthy_env(CVLFACE_DOWNLOAD_ENV):
        return True
    try:
        from huggingface_hub import snapshot_download  # type: ignore

        for repo in [CVLFACE_MODEL_REPO, CVLFACE_ALIGNER_REPO]:
            snapshot_download(repo, local_files_only=True)
    except Exception:
        return False
    return True


def _truthy_env(name: str) -> bool:
    import os

    return str(os.environ.get(name) or "").strip().casefold() in {"1", "true", "yes", "on"}


def _arcface_available() -> bool:
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _onnxruntime_providers() -> list[str]:
    try:
        import onnxruntime as ort  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ONNX Runtime is required for ArcFace face embeddings. Install with "
            "`python -m pip install -e '.[face-ai]'`."
        ) from exc
    available = set(ort.get_available_providers())
    preferred = []
    if platform.system() == "Darwin" and "CoreMLExecutionProvider" in available:
        preferred.append("CoreMLExecutionProvider")
    for provider in ["CUDAExecutionProvider", "CPUExecutionProvider"]:
        if provider in available:
            preferred.append(provider)
    if not preferred:
        raise RuntimeError("ONNX Runtime has no supported execution providers for face embeddings")
    return preferred


def _insightface_observation_score(observation: Any) -> float:
    bbox = getattr(observation, "bbox", None)
    area = 1.0
    if bbox is not None and len(bbox) >= 4:
        area = max(1.0, float(bbox[2] - bbox[0]) * float(bbox[3] - bbox[1]))
    det_score = float(getattr(observation, "det_score", 1.0) or 1.0)
    return area * det_score


def _normalized_vector(values: Any) -> list[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


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


def _cluster_feature_rows(rows: list[dict[str, Any]], *, max_distance: float) -> dict[str, Any]:
    """Constrained average-linkage over seed faces, then join-only attachment.

    Replaces the order-dependent greedy centroid pass: agglomerative
    average-linkage is deterministic, repairs the old never-merge/centroid-
    drift pathologies, and honors cannot-link constraints (two faces sharing a
    keyframe are different people, so their clusters may never merge). Faces
    below the seed weight join the nearest compatible cluster or stay
    unclustered — they can inherit an identity but never found one.
    """

    for row in rows:
        row.setdefault("weight", 1.0)
        row.setdefault("frame_key", None)
        row.setdefault("seed", True)
    seeds = [row for row in rows if row["seed"]]
    joiners = [row for row in rows if not row["seed"]]

    clusters, blocked = _constrained_average_linkage(seeds, max_distance=max_distance)

    joined = 0
    for row in joiners:
        target = _best_join_cluster(row, clusters, max_distance=max_distance)
        if target is None:
            continue
        target["faces"].append(row["face"])
        target["features"].append(row["feature"])
        target["weights"].append(row["weight"])
        if row["frame_key"]:
            target["frame_keys"].add(row["frame_key"])
        target["centroid"] = _weighted_centroid(target["features"], target["weights"])
        joined += 1

    return {
        "clusters": clusters,
        "joined_without_seeding": joined,
        "cannot_link_merges_blocked": blocked,
    }


def _constrained_average_linkage(
    rows: list[dict[str, Any]], *, max_distance: float
) -> tuple[list[dict[str, Any]], int]:
    if not rows:
        return [], 0
    try:
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for face clustering. Install with "
            "`python -m pip install -e '.[vision]'`."
        ) from exc

    count = len(rows)
    vectors = np.asarray([row["feature"] for row in rows], dtype=np.float64)
    distances = 1.0 - vectors @ vectors.T
    np.clip(distances, 0.0, 2.0, out=distances)

    members: list[list[int] | None] = [[index] for index in range(count)]
    frame_keys: list[set | None] = [
        {rows[index]["frame_key"]} if rows[index]["frame_key"] else set() for index in range(count)
    ]
    sizes = np.ones(count)
    working = distances.copy()
    np.fill_diagonal(working, np.inf)

    blocked = 0
    for left in range(count):
        for right in range(left + 1, count):
            if frame_keys[left] & frame_keys[right]:  # type: ignore[operator]
                if working[left, right] <= max_distance:
                    blocked += 1
                working[left, right] = np.inf
                working[right, left] = np.inf

    while True:
        flat_index = int(np.argmin(working))
        left, right = divmod(flat_index, count)
        if not np.isfinite(working[left, right]) or working[left, right] > max_distance:
            break
        if right < left:
            left, right = right, left
        # Lance-Williams average-linkage update of `left`; retire `right`.
        size_left, size_right = sizes[left], sizes[right]
        merged_row = (size_left * working[left, :] + size_right * working[right, :]) / (
            size_left + size_right
        )
        merged_row[left] = np.inf
        working[left, :] = merged_row
        working[:, left] = merged_row
        working[right, :] = np.inf
        working[:, right] = np.inf
        sizes[left] = size_left + size_right
        members[left].extend(members[right])  # type: ignore[union-attr]
        members[right] = None
        frame_keys[left] |= frame_keys[right]  # type: ignore[operator]
        frame_keys[right] = None
        for other in range(count):
            if members[other] is None or other == left:
                continue
            if frame_keys[left] & frame_keys[other]:  # type: ignore[operator]
                if np.isfinite(working[left, other]) and working[left, other] <= max_distance:
                    blocked += 1
                working[left, other] = np.inf
                working[other, left] = np.inf

    clusters = []
    for index in range(count):
        member_indices = members[index]
        if member_indices is None:
            continue
        ordered = sorted(member_indices)
        max_internal = 0.0
        for position, left in enumerate(ordered):
            for right in ordered[position + 1 :]:
                max_internal = max(max_internal, float(distances[left, right]))
        cluster_rows = [rows[i] for i in ordered]
        features = [row["feature"] for row in cluster_rows]
        weights = [row["weight"] for row in cluster_rows]
        clusters.append(
            {
                "faces": [row["face"] for row in cluster_rows],
                "features": features,
                "weights": weights,
                "frame_keys": set(frame_keys[index] or set()),
                "centroid": _weighted_centroid(features, weights),
                "max_observed_distance": round(max_internal, 4),
                "_order": ordered[0],
            }
        )
    clusters.sort(key=lambda cluster: cluster["_order"])
    for cluster in clusters:
        cluster.pop("_order", None)
    return clusters, blocked


def _best_join_cluster(
    row: dict[str, Any], clusters: list[dict[str, Any]], *, max_distance: float
) -> dict[str, Any] | None:
    best = None
    best_distance = None
    for cluster in clusters:
        if row["frame_key"] and row["frame_key"] in cluster["frame_keys"]:
            continue
        distance = _cosine_distance(row["feature"], cluster["centroid"])
        if distance > max_distance:
            continue
        if best_distance is None or distance < best_distance:
            best = cluster
            best_distance = distance
    return best


def _frame_key(face: dict[str, Any]) -> str | None:
    subject_id = str(face.get("source_subject_id") or "")
    subject_type = str(face.get("source_subject_type") or "")
    if not subject_id:
        return None
    return f"{subject_type}:{subject_id}"


def _weighted_centroid(features: list[list[float]], weights: list[float]) -> list[float]:
    if not features:
        return []
    dims = len(features[0])
    total = sum(weights) or 1.0
    values = [
        sum(feature[index] * weight for feature, weight in zip(features, weights, strict=False)) / total
        for index in range(dims)
    ]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


_DEFAULT_CLUSTER_LABEL = re.compile(r"^Face cluster \d+$")


def _assign_cluster_ids(
    clusters: list[dict[str, Any]],
    *,
    previous_clusters: list[dict[str, Any]],
    previous_observations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Cluster-id succession: inherit the previous id with max anchor Jaccard.

    Anchor keys (media + time + bbox bucket) survive both re-clustering and
    re-detection, so human decisions bound to a cluster id keep pointing at
    the same people. A human-labeled cluster whose members scatter below the
    Jaccard floor is a loud succession conflict — the closest new cluster is
    flagged needs_review instead of silently renumbering.
    """

    previous_keys: dict[str, set] = {}
    previous_meta: dict[str, dict[str, Any]] = {}
    for previous in previous_clusters:
        previous_id = str(previous.get("id") or "")
        if not previous_id:
            continue
        keys = set()
        for face_id in previous.get("face_observation_ids") or []:
            observation = previous_observations.get(str(face_id))
            if observation:
                key = anchor_key(observation)
                if key:
                    keys.add(key)
        previous_meta[previous_id] = previous
        if keys:
            previous_keys[previous_id] = keys

    new_keys: list[set] = []
    for cluster in clusters:
        keys = set()
        for face in cluster["faces"]:
            key = anchor_key(face)
            if key:
                keys.add(key)
        new_keys.append(keys)

    pairs = []
    for cluster_index, keys in enumerate(new_keys):
        if not keys:
            continue
        for previous_id, keys_before in previous_keys.items():
            intersection = len(keys & keys_before)
            if not intersection:
                continue
            jaccard = intersection / len(keys | keys_before)
            pairs.append((jaccard, intersection, previous_id, cluster_index))
    pairs.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))

    best_for_previous: dict[str, tuple[float, int]] = {}
    assigned_previous: set[str] = set()
    assigned_new: dict[int, str] = {}
    for jaccard, _intersection, previous_id, cluster_index in pairs:
        best_for_previous.setdefault(previous_id, (jaccard, cluster_index))
        if previous_id in assigned_previous or cluster_index in assigned_new:
            continue
        if jaccard >= SUCCESSION_MIN_JACCARD:
            assigned_previous.add(previous_id)
            assigned_new[cluster_index] = previous_id

    used_indices = [0]
    for previous_id in previous_meta:
        try:
            used_indices.append(int(previous_id.rsplit("_", 1)[-1]))
        except ValueError:
            continue
    next_index = max(used_indices) + 1

    inherited = 0
    created = 0
    for cluster_index, cluster in enumerate(clusters):
        previous_id = assigned_new.get(cluster_index)
        if previous_id:
            cluster["cluster_id"] = previous_id
            inherited += 1
            previous = previous_meta[previous_id]
            fields: dict[str, Any] = {}
            label = str(previous.get("label") or "")
            if label and not _DEFAULT_CLUSTER_LABEL.match(label):
                fields["label"] = label
            if previous.get("linked_person_group_id"):
                fields["linked_person_group_id"] = previous["linked_person_group_id"]
                fields["review_status"] = "confirmed"
            if previous.get("review_correction_ids"):
                fields["review_correction_ids"] = list(previous["review_correction_ids"])
            cluster["inherited_fields"] = fields
        else:
            cluster["cluster_id"] = f"face_cluster_{next_index:06d}"
            next_index += 1
            created += 1

    conflicts = []
    for previous_id, previous in previous_meta.items():
        if previous_id in assigned_previous:
            continue
        if not _human_labeled_cluster(previous):
            continue
        best = best_for_previous.get(previous_id)
        conflict = {
            "previous_cluster_id": previous_id,
            "label": str(previous.get("label") or ""),
            "linked_person_group_id": str(previous.get("linked_person_group_id") or ""),
            "best_jaccard": round(best[0], 3) if best else 0.0,
            "best_new_cluster_id": clusters[best[1]]["cluster_id"] if best else "",
        }
        conflicts.append(conflict)
        if best:
            target = clusters[best[1]]
            fields = target.setdefault("inherited_fields", {})
            fields["review_status"] = "needs_review"
            notes = fields.setdefault("notes", [])
            notes.append(
                f"human-labeled cluster '{conflict['label'] or previous_id}' split during "
                "re-cluster; re-confirm identity"
            )

    return {"inherited": inherited, "created": created, "conflicts": conflicts}


def _human_labeled_cluster(cluster: dict[str, Any]) -> bool:
    if cluster.get("linked_person_group_id"):
        return True
    if str(cluster.get("review_status") or "") == "confirmed":
        return True
    label = str(cluster.get("label") or "")
    return bool(label) and not _DEFAULT_CLUSTER_LABEL.match(label)


def _cluster_record(
    cluster_id: str,
    faces: list[dict[str, Any]],
    *,
    max_distance: float,
    candidate_people: list[dict[str, Any]],
    method: str,
    feature_model: str,
) -> dict[str, Any]:
    source_video_ids = _unique_items(str(face.get("source_video_id") or "") for face in faces)
    face_ids = [str(face.get("id")) for face in faces if face.get("id")]
    starts = [_number_or_none(face.get("start_s") or face.get("time_s")) for face in faces]
    ends = [_number_or_none(face.get("end_s") or face.get("time_s")) for face in faces]
    first_start = min([value for value in starts if value is not None], default=None)
    last_end = max([value for value in ends if value is not None], default=None)
    representative = _representative_face(faces)
    review_status = "needs_review" if candidate_people or len(faces) > 1 else "unreviewed"
    quality = _cluster_quality(faces)
    # The review-only singleton factory is gone: every embeddable face
    # participates in similarity clustering, weighted by quality. The field
    # remains for schema compatibility (detach can empty a cluster).
    review_only = False
    notes = [
        "Face clusters are visual similarity candidates, not confirmed identities.",
        "Candidate people are inferred from event co-occurrence and require review.",
    ]
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
        "method": method,
        "feature_model": feature_model,
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


def _representative_face(faces: list[dict[str, Any]]) -> dict[str, Any]:
    """Best-quality member represents the cluster (thumbnail, review surfaces)."""

    if not faces:
        return {}
    return max(faces, key=lambda face: float(face.get("face_quality_weight") or 0.0))


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

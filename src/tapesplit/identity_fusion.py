"""Multi-signal identity fusion: score person-group pairs for merge likelihood.

People end up split across groups ("Ekaterina / Katya" vs "Ekaterina / Katya / Катя")
because group formation is mention-scoped and deliberately refuses to merge
groups whose accreted alias sets conflict. This module scores every plausible
pair across independent dimensions and emits reviewable merge candidates:

- name:            transliteration + diminutive/hypocorism rules + edit
                   similarity, optionally confirmed by LaBSE cross-script
                   embeddings when the model is available locally.
- face_cluster:    the same face cluster carries identity candidates for both
                   groups (the strongest same-person signal we already have).
- face_embedding:  centroid similarity of face-crop embeddings across the two
                   groups' clusters (availability-gated on the faces backend).
- voice:           ECAPA voiceprint similarity over each person's attributed
                   speaker segments (availability-gated on speechbrain+audio).
- co_occurrence:   shared events as a weak positive; same-frame co-presence is
                   a HARD NEGATIVE — two faces in one frame cannot be the same
                   person, so fusion blocks the merge outright.

Dimension scores fuse via weighted log-odds. A pair supported by only one
dimension is capped below the merge_person auto-accept floor, so single-signal
candidates always stay human-reviewed. Weights can be tuned per project via
``review_policy.json`` under ``identity_fusion_weights`` (calibration seam).

Temporal/appearance-span priors are intentionally NOT scored yet: mention
spans are not discriminative for same-vs-different person on this data; the
dimension is reserved until face-time coverage makes it honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Callable

from tapesplit.grouping import (
    PERSON_NAME_EQUIVALENT_KEYS,
    ROLE_PERSON_TOKENS,
    _person_spelling_key,
    _transliterate_cyrillic,
)
from tapesplit.storage import read_jsonl

PERSON_MERGE_CANDIDATES_FILENAME = "person_merge_candidates.jsonl"

# Fusion weights (log-odds multipliers). Tunable via review_policy.json
# "identity_fusion_weights"; keep keys in sync with _Dimension.name values.
DEFAULT_FUSION_WEIGHTS = {
    "name": 1.0,
    "face_cluster": 1.2,
    "face_embedding": 0.9,
    "voice": 0.8,
    "co_occurrence": 0.4,
    "age_trajectory": 0.7,
}

# Base rate: most person-group pairs are different people.
PRIOR_PROBABILITY = 0.08
# A single fired dimension can never clear the merge_person auto-accept floor.
SINGLE_DIMENSION_CONFIDENCE_CAP = 0.65
# Strong non-name signals with unrelated label names smell like contaminated
# clusters; such pairs stay review-only regardless of fused confidence.
NAME_DISAGREEMENT_CONFIDENCE_CAP = 0.65
NAME_AGREEMENT_MIN_SCORE = 0.4
BLOCKED_CONFIDENCE_CAP = 0.2
# Hard-negative co-presence requires this many distinct frames to tolerate a
# single contaminated cluster/detection.
CO_PRESENCE_FRAME_THRESHOLD = 2
CO_PRESENCE_TIME_TOLERANCE_S = 0.75
# Cluster attribution strength required before a cluster's observations can
# hard-block a merge (weak candidacy must not veto).
HARD_NEGATIVE_MIN_CLUSTER_CONFIDENCE = 0.6
SHARED_CLUSTER_MIN_CONFIDENCE = 0.3
FACE_EMBEDDING_MAX_OBSERVATIONS = 6
VOICE_MAX_SEGMENTS = 6
VOICE_MIN_SEGMENT_S = 2.5
VOICE_MAX_SEGMENT_S = 15.0
# Cheap-signal threshold that unlocks the expensive dimensions.
HEAVY_DIMENSION_GATE = 0.4

# Age-trajectory dimension: both groups need this many dated age samples
# before their birth-year posteriors say anything.
AGE_TRAJECTORY_MIN_SAMPLES = 3
# Incompatibility veto (a child and an adult sharing an era can never be one
# person) fires only on an extreme, well-supported gap — VHS age estimates
# are noisy, so anything short of extreme just scores low instead.
AGE_VETO_MIN_BIRTH_YEAR_GAP = 15.0
AGE_VETO_MAX_SIGMA = 4.0

# Russian hypocorism suffixes, longest first; stems must stay >= 3 chars.
DIMINUTIVE_SUFFIXES = (
    "yushka",
    "ushka",
    "ochka",
    "echka",
    "enka",
    "onka",
    "yusha",
    "usha",
    "asha",
    "ik",
    "ka",
    "ya",
    "a",
)
MIN_STEM_LENGTH = 3


@dataclass
class _Dimension:
    name: str
    score: float | None
    detail: str
    blocking: bool = False


@dataclass
class FusionResult:
    confidence: float
    blocked: bool
    blocked_reason: str
    dimensions: list[_Dimension] = field(default_factory=list)

    @property
    def fired(self) -> list[_Dimension]:
        return [dim for dim in self.dimensions if dim.score is not None]


def check_identity_fusion_config() -> dict[str, Any]:
    return {
        "identity_fusion_labse": _labse_available(),
        "identity_fusion_face_embedding": _face_embedder_available(),
        "identity_fusion_voice": _speechbrain_available(),
    }


def build_person_merge_candidates(
    project_dir: Path,
    *,
    use_labse: bool | str = "auto",
    use_face_embeddings: bool | str = "auto",
    use_voice: bool | str = "auto",
    max_candidates: int = 60,
) -> dict[str, Any]:
    """Score person-group pairs and write person_merge_candidates.jsonl."""

    project = project_dir.expanduser().resolve()
    groups = [g for g in read_jsonl(project / "people_groups.jsonl") if isinstance(g, dict)]
    persons = [g for g in groups if _mergeable_group(g)]
    identity_candidates = read_jsonl(project / "face_identity_candidates.jsonl")
    clusters = read_jsonl(project / "face_clusters.jsonl")
    observations = read_jsonl(project / "face_observations.jsonl")
    speaker_segments = read_jsonl(project / "speaker_segments.jsonl")

    weights = _fusion_weights(project)
    cluster_links = _cluster_links(identity_candidates, clusters)
    obs_by_cluster = _observations_by_cluster(observations)
    labse = _LabseScorer(enabled=use_labse)
    face_embedder = _FaceCentroidScorer(project, obs_by_cluster, enabled=use_face_embeddings)
    voice = _VoiceCentroidScorer(project, speaker_segments, enabled=use_voice)
    age_models = _load_age_models(project)

    scored: list[dict[str, Any]] = []
    for index_a in range(len(persons)):
        for index_b in range(index_a + 1, len(persons)):
            group_a, group_b = persons[index_a], persons[index_b]
            result = _score_pair(
                group_a,
                group_b,
                cluster_links=cluster_links,
                obs_by_cluster=obs_by_cluster,
                labse=labse,
                face_embedder=face_embedder,
                voice=voice,
                age_models=age_models,
                weights=weights,
            )
            if result is None:
                continue
            scored.append(_candidate_row(group_a, group_b, result))

    scored.sort(key=lambda row: (-row["confidence"], row["person_group_id_a"], row["person_group_id_b"]))
    scored = scored[:max_candidates]
    for index, row in enumerate(scored, start=1):
        row["id"] = f"person_merge_candidate_{index:06d}"

    output = project / PERSON_MERGE_CANDIDATES_FILENAME
    with output.open("w", encoding="utf-8") as handle:
        for row in scored:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")

    return {
        "person_groups_considered": len(persons),
        "candidates": len(scored),
        "blocked": sum(1 for row in scored if row["blocked"]),
        "dimensions_available": {
            "labse": labse.available,
            "face_embedding": face_embedder.available,
            "voice": voice.available,
        },
        "output": str(output),
    }


def _score_pair(
    group_a: dict[str, Any],
    group_b: dict[str, Any],
    *,
    cluster_links: dict[str, dict[str, float]],
    obs_by_cluster: dict[str, list[dict[str, Any]]],
    labse: "_LabseScorer",
    face_embedder: "_FaceCentroidScorer",
    voice: "_VoiceCentroidScorer",
    age_models: dict[str, dict[str, Any]] | None = None,
    weights: dict[str, float],
) -> FusionResult | None:
    name = _name_dimension(group_a, group_b, labse)
    shared = _shared_cluster_dimension(group_a, group_b, cluster_links)
    co_presence = _co_presence_block(group_a, group_b, cluster_links, obs_by_cluster)
    co_occurrence = _co_occurrence_dimension(group_a, group_b)
    age = _age_trajectory_dimension(group_a, group_b, age_models or {})

    cheap_signal = max(name.score or 0.0, shared.score or 0.0)
    dimensions = [name, shared, co_occurrence, age]
    if co_presence is not None:
        dimensions.append(co_presence)

    if cheap_signal >= HEAVY_DIMENSION_GATE and (co_presence is None or not co_presence.blocking):
        dimensions.append(face_embedder.dimension(group_a, group_b, cluster_links))
        dimensions.append(voice.dimension(group_a, group_b))

    if cheap_signal < HEAVY_DIMENSION_GATE and (co_presence is None or not co_presence.blocking):
        # Nothing links these people; don't emit noise candidates.
        return None

    result = _fuse(dimensions, weights)
    if result.confidence > NAME_DISAGREEMENT_CONFIDENCE_CAP and not _label_names_agree(group_a, group_b):
        result.confidence = NAME_DISAGREEMENT_CONFIDENCE_CAP
        result.dimensions.append(
            _Dimension(
                "name_guard",
                None,
                "label names are unrelated; capped to review-only despite other signals",
            )
        )
    return result


def _label_names_agree(group_a: dict[str, Any], group_b: dict[str, Any]) -> bool:
    for alias_a in _label_alias_keys(group_a):
        for alias_b in _label_alias_keys(group_b):
            if _name_pair_score(alias_a, alias_b)[0] >= NAME_AGREEMENT_MIN_SCORE:
                return True
    return False


def _fuse(dimensions: list[_Dimension], weights: dict[str, float]) -> FusionResult:
    blocking = [dim for dim in dimensions if dim.blocking]
    fired = [dim for dim in dimensions if dim.score is not None]
    logit = _logit(PRIOR_PROBABILITY)
    for dim in fired:
        weight = float(weights.get(dim.name, DEFAULT_FUSION_WEIGHTS.get(dim.name, 0.5)))
        logit += weight * _logit(min(max(dim.score, 0.02), 0.98))
    confidence = 1.0 / (1.0 + math.exp(-logit))
    if len(fired) < 2:
        confidence = min(confidence, SINGLE_DIMENSION_CONFIDENCE_CAP)
    blocked = bool(blocking)
    if blocked:
        confidence = min(confidence, BLOCKED_CONFIDENCE_CAP)
    return FusionResult(
        confidence=round(confidence, 4),
        blocked=blocked,
        blocked_reason=blocking[0].detail if blocking else "",
        dimensions=dimensions,
    )


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _fusion_weights(project: Path) -> dict[str, float]:
    policy_path = project / "review_policy.json"
    weights = dict(DEFAULT_FUSION_WEIGHTS)
    if policy_path.exists():
        try:
            payload = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return weights
        tuned = payload.get("identity_fusion_weights")
        if isinstance(tuned, dict):
            for key, value in tuned.items():
                if key in weights and isinstance(value, (int, float)) and 0.0 <= float(value) <= 3.0:
                    weights[key] = float(value)
    return weights


# --- name dimension -------------------------------------------------------


# Alias lists accrete co-mentioned names ("Ekaterina" ending up on a Filip group);
# only label-derived aliases are authoritative. Matches that rely on a stray
# alias are capped here so they corroborate but can never drive a merge.
UNCONFIRMED_ALIAS_SCORE_CAP = 0.6


def _name_dimension(group_a: dict[str, Any], group_b: dict[str, Any], labse: "_LabseScorer") -> _Dimension:
    aliases_a = _name_aliases(group_a)
    aliases_b = _name_aliases(group_b)
    if not aliases_a or not aliases_b:
        return _Dimension("name", None, "no comparable name aliases")

    label_aliases_a = _label_alias_keys(group_a)
    label_aliases_b = _label_alias_keys(group_b)

    best = 0.0
    best_pair = ("", "")
    best_reason = ""
    agreements = 0
    for alias_a in aliases_a:
        for alias_b in aliases_b:
            score, reason = _name_pair_score(alias_a, alias_b)
            if score > 0 and (
                alias_a.casefold() not in label_aliases_a or alias_b.casefold() not in label_aliases_b
            ):
                score = min(score, UNCONFIRMED_ALIAS_SCORE_CAP)
                reason = f"{reason}; stray alias, capped"
            if score >= 0.85:
                agreements += 1
            if score > best:
                best, best_pair, best_reason = score, (alias_a, alias_b), reason

    if best < 0.4 and labse.available:
        rescued = labse.best_pair_similarity(aliases_a, aliases_b)
        if rescued is not None:
            cosine, pair = rescued
            if cosine >= 0.85:
                best = max(best, 0.55 + (cosine - 0.85))
                best_pair = pair
                best_reason = f"LaBSE cross-script similarity {cosine:.2f}"

    if best <= 0.0:
        return _Dimension("name", None, "no linguistic relation between alias sets")
    if agreements > 1:
        best = min(1.0, best + 0.03 * min(agreements - 1, 3))
    detail = f"'{best_pair[0]}' ~ '{best_pair[1]}' ({best_reason}; {agreements} agreeing alias pair(s))"
    return _Dimension("name", round(best, 4), detail)


def _name_pair_score(alias_a: str, alias_b: str) -> tuple[float, str]:
    norm_a, norm_b = _normalize_name(alias_a), _normalize_name(alias_b)
    if not norm_a or not norm_b:
        return 0.0, "unusable alias"
    if _is_role_name(norm_a) or _is_role_name(norm_b):
        return 0.0, "role token, not a name"
    if norm_a == norm_b:
        return 1.0, "identical after transliteration"
    key_a, key_b = _person_spelling_key(norm_a), _person_spelling_key(norm_b)
    if key_a and key_a == key_b:
        return 0.92, "same spelling key"
    if _diminutive_related(norm_a, norm_b):
        return 0.95, "diminutive/formal pair"
    stem_a, stem_b = _diminutive_stem(norm_a), _diminutive_stem(norm_b)
    if stem_a and stem_a == stem_b:
        return 0.85, f"shared hypocorism stem '{stem_a}'"
    if min(len(norm_a), len(norm_b)) >= 4:
        similarity = _edit_similarity(norm_a, norm_b)
        if similarity >= 0.8:
            return round(0.6 + (similarity - 0.8), 4), f"edit similarity {similarity:.2f}"
    return 0.0, "unrelated"


def _normalize_name(value: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", str(value)).casefold().strip()
    text = _transliterate_cyrillic(text)
    text = re.sub(r"[^a-z ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_role_name(normalized: str) -> bool:
    return bool(normalized) and all(token in ROLE_PERSON_TOKENS for token in normalized.split())


def _diminutive_related(norm_a: str, norm_b: str) -> bool:
    canon_a = PERSON_NAME_EQUIVALENT_KEYS.get(norm_a)
    canon_b = PERSON_NAME_EQUIVALENT_KEYS.get(norm_b)
    return canon_a is not None and canon_a == canon_b


def _diminutive_stem(normalized: str) -> str:
    if " " in normalized:
        return ""
    for suffix in DIMINUTIVE_SUFFIXES:
        if normalized.endswith(suffix):
            stem = normalized[: -len(suffix)]
            if len(stem) >= MIN_STEM_LENGTH:
                return stem
            return ""
    return ""


def _edit_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return 1.0 - previous[-1] / max(len(a), len(b))


def _name_aliases(group: dict[str, Any]) -> list[str]:
    aliases = [str(a) for a in group.get("aliases") or [] if str(a).strip()]
    label = str(group.get("label") or "")
    aliases.extend(part.strip() for part in label.split("/") if part.strip())
    unique: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if "/" in alias:
            continue  # composite labels leaked into aliases; components are present separately
        key = alias.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(alias)
    return unique[:12]


def _label_alias_keys(group: dict[str, Any]) -> set[str]:
    label = str(group.get("label") or "")
    return {part.strip().casefold() for part in label.split("/") if part.strip()}


def _mergeable_group(group: dict[str, Any]) -> bool:
    if str(group.get("kind") or "") == "role_candidate":
        return False
    label = _normalize_name(str(group.get("label") or ""))
    return bool(label) and not _is_role_name(label)


# --- face dimensions ------------------------------------------------------


def _cluster_links(
    identity_candidates: list[Any], clusters: list[Any]
) -> dict[str, dict[str, float]]:
    """cluster_id -> {person_group_id: attribution confidence}."""

    links: dict[str, dict[str, float]] = {}
    for candidate in identity_candidates:
        if not isinstance(candidate, dict):
            continue
        cluster_id = str(candidate.get("face_cluster_id") or "")
        person_id = str(candidate.get("person_group_id") or "")
        confidence = candidate.get("confidence")
        if not cluster_id or not person_id or not isinstance(confidence, (int, float)):
            continue
        if str(candidate.get("review_status") or "") == "rejected":
            continue
        by_person = links.setdefault(cluster_id, {})
        by_person[person_id] = max(by_person.get(person_id, 0.0), float(confidence))
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        linked = str(cluster.get("linked_person_group_id") or "")
        if linked:
            by_person = links.setdefault(str(cluster.get("id") or ""), {})
            by_person[linked] = max(by_person.get(linked, 0.0), 0.9)
    return links


def _clusters_for_person(
    person_group_id: str, cluster_links: dict[str, dict[str, float]], *, min_confidence: float
) -> dict[str, float]:
    return {
        cluster_id: by_person[person_group_id]
        for cluster_id, by_person in cluster_links.items()
        if by_person.get(person_group_id, 0.0) >= min_confidence
    }


def _shared_cluster_dimension(
    group_a: dict[str, Any], group_b: dict[str, Any], cluster_links: dict[str, dict[str, float]]
) -> _Dimension:
    id_a, id_b = str(group_a.get("id")), str(group_b.get("id"))
    clusters_a = _clusters_for_person(id_a, cluster_links, min_confidence=SHARED_CLUSTER_MIN_CONFIDENCE)
    clusters_b = _clusters_for_person(id_b, cluster_links, min_confidence=SHARED_CLUSTER_MIN_CONFIDENCE)
    shared = sorted(set(clusters_a) & set(clusters_b))
    if not clusters_a or not clusters_b:
        return _Dimension("face_cluster", None, "no face clusters attributed")
    if not shared:
        return _Dimension("face_cluster", None, "no shared face-cluster candidacy")
    strength = max(min(clusters_a[c], clusters_b[c]) for c in shared)
    score = 0.5 + 0.5 * strength
    detail = f"{len(shared)} shared cluster(s) ({', '.join(shared[:3])}), min-candidacy {strength:.2f}"
    return _Dimension("face_cluster", round(score, 4), detail)


def _observations_by_cluster(observations: list[Any]) -> dict[str, list[dict[str, Any]]]:
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        if isinstance(obs, dict) and obs.get("face_cluster_id"):
            by_cluster.setdefault(str(obs["face_cluster_id"]), []).append(obs)
    return by_cluster


def _co_presence_block(
    group_a: dict[str, Any],
    group_b: dict[str, Any],
    cluster_links: dict[str, dict[str, float]],
    obs_by_cluster: dict[str, list[dict[str, Any]]],
) -> _Dimension | None:
    id_a, id_b = str(group_a.get("id")), str(group_b.get("id"))
    clusters_a = _clusters_for_person(id_a, cluster_links, min_confidence=HARD_NEGATIVE_MIN_CLUSTER_CONFIDENCE)
    clusters_b = _clusters_for_person(id_b, cluster_links, min_confidence=HARD_NEGATIVE_MIN_CLUSTER_CONFIDENCE)
    exclusive_a = set(clusters_a) - set(clusters_b)
    exclusive_b = set(clusters_b) - set(clusters_a)
    if not exclusive_a or not exclusive_b:
        return None

    frames: set[str] = set()
    frame_details: list[str] = []
    obs_a = [obs for cluster in exclusive_a for obs in obs_by_cluster.get(cluster, [])]
    obs_b = [obs for cluster in exclusive_b for obs in obs_by_cluster.get(cluster, [])]
    for a in obs_a:
        for b in obs_b:
            if _same_frame(a, b):
                key = str(a.get("source_subject_id") or f"{a.get('source_video_id')}@{a.get('time_s')}")
                if key not in frames:
                    frames.add(key)
                    frame_details.append(key)

    if len(frames) >= CO_PRESENCE_FRAME_THRESHOLD:
        detail = (
            f"faces of both people appear together in {len(frames)} frame(s) "
            f"(e.g. {', '.join(frame_details[:3])}) — cannot be the same person"
        )
        return _Dimension("co_presence", 0.02, detail, blocking=True)
    return None


def _same_frame(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("id") == b.get("id"):
        return False
    subject_a, subject_b = a.get("source_subject_id"), b.get("source_subject_id")
    if subject_a and subject_a == subject_b:
        return True
    if a.get("source_video_id") and a.get("source_video_id") == b.get("source_video_id"):
        time_a, time_b = a.get("time_s"), b.get("time_s")
        if isinstance(time_a, (int, float)) and isinstance(time_b, (int, float)):
            return abs(float(time_a) - float(time_b)) <= CO_PRESENCE_TIME_TOLERANCE_S
    return False


def _co_occurrence_dimension(group_a: dict[str, Any], group_b: dict[str, Any]) -> _Dimension:
    events_a = {str(e) for e in group_a.get("canonical_event_ids") or []}
    events_b = {str(e) for e in group_b.get("canonical_event_ids") or []}
    if not events_a or not events_b:
        return _Dimension("co_occurrence", None, "no event coverage")
    shared = events_a & events_b
    if not shared:
        return _Dimension("co_occurrence", None, "no shared events")
    jaccard = len(shared) / len(events_a | events_b)
    score = min(0.85, 0.5 + jaccard)
    detail = f"{len(shared)} shared event(s), jaccard {jaccard:.2f}"
    return _Dimension("co_occurrence", round(score, 4), detail)


# --- age trajectory dimension ----------------------------------------------


def _load_age_models(project: Path) -> dict[str, dict[str, Any]]:
    try:
        from tapesplit.age_timeline import load_person_age_models

        return load_person_age_models(project)
    except Exception:
        return {}


def _age_trajectory_dimension(
    group_a: dict[str, Any], group_b: dict[str, Any], age_models: dict[str, dict[str, Any]]
) -> _Dimension:
    """Birth-year consistency: one aging person leaves one trajectory.

    A 4-year-old on the 1998 tapes and a 10-year-old on the 2004 tapes imply
    the same birth year — merge evidence face embeddings cannot provide across
    that gap. A child and an adult whose eras overlap imply birth years ~20+
    years apart — with enough well-dated samples that is a physical veto.
    """

    model_a = age_models.get(str(group_a.get("id") or ""))
    model_b = age_models.get(str(group_b.get("id") or ""))
    if not model_a or not model_b:
        return _Dimension("age_trajectory", None, "no age model for one or both people")
    if (
        str(model_a.get("attribution_basis") or "confirmed") != "confirmed"
        or str(model_b.get("attribution_basis") or "confirmed") != "confirmed"
    ):
        # Candidate-attributed models inherit event-co-occurrence pollution
        # (an adult filmed at "Filip's birthday" reads as Filip); age evidence
        # only fires once humans have confirmed cluster identities.
        return _Dimension("age_trajectory", None, "age model not human-confirmed yet")
    if (
        int(model_a.get("sample_count") or 0) < AGE_TRAJECTORY_MIN_SAMPLES
        or int(model_b.get("sample_count") or 0) < AGE_TRAJECTORY_MIN_SAMPLES
    ):
        return _Dimension("age_trajectory", None, "too few dated age samples")

    birth_a, birth_b = float(model_a["birth_year"]), float(model_b["birth_year"])
    sigma_a, sigma_b = float(model_a.get("sigma") or 3.0), float(model_b.get("sigma") or 3.0)
    delta = abs(birth_a - birth_b)
    combined_sigma = max(1.0, math.sqrt(sigma_a**2 + sigma_b**2))
    z = delta / combined_sigma

    eras_overlap = bool(
        set(model_a.get("observed_years") or []) & set(model_b.get("observed_years") or [])
    )
    if (
        delta >= AGE_VETO_MIN_BIRTH_YEAR_GAP
        and sigma_a <= AGE_VETO_MAX_SIGMA
        and sigma_b <= AGE_VETO_MAX_SIGMA
        and eras_overlap
    ):
        detail = (
            f"birth years ~{birth_a:.0f} vs ~{birth_b:.0f} with overlapping eras — "
            "a child and an adult cannot be the same person"
        )
        return _Dimension("age_trajectory", 0.02, detail, blocking=True)

    score = max(0.05, min(0.95, math.exp(-0.5 * z * z)))
    detail = (
        f"birth-year posteriors {birth_a:.0f}±{sigma_a:.1f} vs {birth_b:.0f}±{sigma_b:.1f} "
        f"(z={z:.2f}, {model_a['sample_count']}+{model_b['sample_count']} samples)"
    )
    return _Dimension("age_trajectory", round(score, 4), detail)


# --- gated heavy scorers --------------------------------------------------


def _labse_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def _face_embedder_available() -> bool:
    try:
        from tapesplit.face_clustering import check_face_embedding_config

        return bool(check_face_embedding_config().get("face_embedding_arcface"))
    except Exception:
        return False


def _speechbrain_available() -> bool:
    try:
        import speechbrain  # noqa: F401
    except Exception:
        return False
    return True


def _resolve_enabled(enabled: bool | str, probe: Callable[[], bool]) -> bool:
    if enabled is True:
        return probe()
    if enabled is False:
        return False
    return probe()


class _LabseScorer:
    """Cross-script name similarity via sentence-transformers/LaBSE.

    Only used to rescue borderline pairs the rules could not relate. The model
    is loaded lazily from the local cache; downloads happen only when
    TAPESPLIT_FUSION_ALLOW_DOWNLOAD=1 so builds never stall on 1.8GB pulls.
    """

    MODEL_NAME = "sentence-transformers/LaBSE"

    def __init__(self, *, enabled: bool | str = "auto") -> None:
        self.available = _resolve_enabled(enabled, _labse_available)
        self._model: Any = None
        self._cache: dict[str, Any] = {}

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        import os

        from sentence_transformers import SentenceTransformer

        allow_download = os.environ.get("TAPESPLIT_FUSION_ALLOW_DOWNLOAD") == "1"
        try:
            self._model = SentenceTransformer(self.MODEL_NAME, local_files_only=not allow_download)
        except Exception:
            self.available = False
            raise
        return self._model

    def best_pair_similarity(
        self, aliases_a: list[str], aliases_b: list[str]
    ) -> tuple[float, tuple[str, str]] | None:
        if not self.available:
            return None
        try:
            model = self._load()
            texts = [*aliases_a, *aliases_b]
            missing = [t for t in texts if t not in self._cache]
            if missing:
                vectors = model.encode(missing, normalize_embeddings=True)
                for text, vector in zip(missing, vectors):
                    self._cache[text] = vector
            best: tuple[float, tuple[str, str]] | None = None
            for a in aliases_a:
                for b in aliases_b:
                    cosine = float((self._cache[a] * self._cache[b]).sum())
                    if best is None or cosine > best[0]:
                        best = (cosine, (a, b))
            return best
        except Exception:
            self.available = False
            return None


class _FaceCentroidScorer:
    """Centroid similarity of face-crop embeddings between two people."""

    def __init__(
        self,
        project: Path,
        obs_by_cluster: dict[str, list[dict[str, Any]]],
        *,
        enabled: bool | str = "auto",
    ) -> None:
        self.project = project
        self.obs_by_cluster = obs_by_cluster
        self.available = _resolve_enabled(enabled, _face_embedder_available)
        self._embedder: Any = None
        self._centroids: dict[str, Any] = {}

    def dimension(
        self, group_a: dict[str, Any], group_b: dict[str, Any], cluster_links: dict[str, dict[str, float]]
    ) -> _Dimension:
        if not self.available:
            return _Dimension("face_embedding", None, "face embedding backend unavailable")
        try:
            centroid_a = self._person_centroid(str(group_a.get("id")), cluster_links)
            centroid_b = self._person_centroid(str(group_b.get("id")), cluster_links)
        except Exception as exc:
            self.available = False
            return _Dimension("face_embedding", None, f"face embedding failed: {type(exc).__name__}")
        if centroid_a is None or centroid_b is None:
            return _Dimension("face_embedding", None, "not enough embeddable face crops")
        cosine = _cosine(centroid_a, centroid_b)
        score = min(1.0, max(0.0, (cosine - 0.25) / 0.5))
        return _Dimension(
            "face_embedding", round(score, 4), f"face centroid cosine {cosine:.2f} across clusters"
        )

    def _person_centroid(self, person_id: str, cluster_links: dict[str, dict[str, float]]) -> Any:
        if person_id in self._centroids:
            return self._centroids[person_id]
        clusters = _clusters_for_person(person_id, cluster_links, min_confidence=0.5)
        rows: list[dict[str, Any]] = []
        for cluster_id in sorted(clusters, key=clusters.get, reverse=True):
            for obs in self.obs_by_cluster.get(cluster_id, []):
                if str(obs.get("face_quality_status") or "ok") == "ok" and obs.get("face_thumbnail_path"):
                    rows.append(obs)
                if len(rows) >= FACE_EMBEDDING_MAX_OBSERVATIONS:
                    break
            if len(rows) >= FACE_EMBEDDING_MAX_OBSERVATIONS:
                break
        if len(rows) < 2:
            self._centroids[person_id] = None
            return None
        import numpy

        if self._embedder is None:
            from tapesplit.face_clustering import _create_face_embedder

            self._embedder = _create_face_embedder("auto")
        vectors = []
        for row in rows:
            try:
                vector = numpy.asarray(self._embedder.feature(self.project, row), dtype=float)
            except Exception:
                continue
            norm = numpy.linalg.norm(vector)
            if norm > 0:
                vectors.append(vector / norm)
        centroid = numpy.mean(vectors, axis=0) if len(vectors) >= 2 else None
        self._centroids[person_id] = centroid
        return centroid


class _VoiceCentroidScorer:
    """ECAPA voiceprint similarity over each person's attributed segments."""

    def __init__(
        self,
        project: Path,
        speaker_segments: list[Any],
        *,
        enabled: bool | str = "auto",
    ) -> None:
        self.project = project
        self.available = _resolve_enabled(enabled, _speechbrain_available)
        self._classifier: Any = None
        self._centroids: dict[str, Any] = {}
        self._segments_by_person: dict[str, list[dict[str, Any]]] = {}
        for segment in speaker_segments:
            if not isinstance(segment, dict):
                continue
            person_id = str(segment.get("person_group_id") or "")
            duration = float(segment.get("end_s") or 0) - float(segment.get("start_s") or 0)
            if person_id and VOICE_MIN_SEGMENT_S <= duration <= VOICE_MAX_SEGMENT_S:
                self._segments_by_person.setdefault(person_id, []).append(segment)

    def dimension(self, group_a: dict[str, Any], group_b: dict[str, Any]) -> _Dimension:
        if not self.available:
            return _Dimension("voice", None, "speechbrain voiceprints unavailable")
        try:
            centroid_a = self._person_centroid(str(group_a.get("id")))
            centroid_b = self._person_centroid(str(group_b.get("id")))
        except Exception as exc:
            self.available = False
            return _Dimension("voice", None, f"voiceprint failed: {type(exc).__name__}")
        if centroid_a is None or centroid_b is None:
            return _Dimension("voice", None, "not enough attributed speech")
        cosine = _cosine(centroid_a, centroid_b)
        score = min(1.0, max(0.0, (cosine - 0.25) / 0.5))
        return _Dimension("voice", round(score, 4), f"voiceprint cosine {cosine:.2f}")

    def _person_centroid(self, person_id: str) -> Any:
        if person_id in self._centroids:
            return self._centroids[person_id]
        segments = self._segments_by_person.get(person_id, [])[:VOICE_MAX_SEGMENTS]
        if len(segments) < 2:
            self._centroids[person_id] = None
            return None
        import numpy
        import torchaudio

        if self._classifier is None:
            from speechbrain.inference.speaker import EncoderClassifier

            cache_dir = self.project / ".cache" / "speechbrain" / "spkrec-ecapa-voxceleb"
            self._classifier = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(cache_dir)
            )
        vectors = []
        for segment in segments:
            wav_path = self.project / "audio" / f"{segment.get('source_video_id')}.wav"
            if not wav_path.exists():
                continue
            info = torchaudio.info(str(wav_path))
            start = int(float(segment["start_s"]) * info.sample_rate)
            frames = int((float(segment["end_s"]) - float(segment["start_s"])) * info.sample_rate)
            waveform, _rate = torchaudio.load(str(wav_path), frame_offset=start, num_frames=frames)
            embedding = self._classifier.encode_batch(waveform).squeeze().detach().numpy()
            norm = numpy.linalg.norm(embedding)
            if norm > 0:
                vectors.append(embedding / norm)
        centroid = numpy.mean(vectors, axis=0) if len(vectors) >= 2 else None
        self._centroids[person_id] = centroid
        return centroid


def _cosine(a: Any, b: Any) -> float:
    import numpy

    denominator = float(numpy.linalg.norm(a) * numpy.linalg.norm(b))
    if denominator == 0:
        return 0.0
    return float(numpy.dot(a, b) / denominator)


# --- candidate rows -------------------------------------------------------


def _candidate_row(group_a: dict[str, Any], group_b: dict[str, Any], result: FusionResult) -> dict[str, Any]:
    # The richer profile is the merge destination (side A); merging the
    # smaller profile into it preserves the most linked history.
    if len(group_b.get("canonical_event_ids") or []) > len(group_a.get("canonical_event_ids") or []):
        group_a, group_b = group_b, group_a
    dimensions = [
        {
            "dimension": dim.name,
            "score": dim.score,
            "detail": dim.detail,
            "blocking": dim.blocking,
        }
        for dim in result.dimensions
    ]
    return {
        "person_group_id_a": str(group_a.get("id")),
        "person_group_id_b": str(group_b.get("id")),
        "label_a": str(group_a.get("label") or ""),
        "label_b": str(group_b.get("label") or ""),
        "confidence": result.confidence,
        "blocked": result.blocked,
        "blocked_reason": result.blocked_reason,
        "dimensions": dimensions,
        "fired_dimensions": [dim.name for dim in result.fired],
        "evidence_ids": sorted(
            {str(e) for g in (group_a, group_b) for e in g.get("evidence_ids") or []}
        )[:20],
        "review_status": "needs_review",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

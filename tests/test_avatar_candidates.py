"""Avatar candidates: quality-ranked, attribution-gated person crops."""

from tapesplit.visualization import (
    AVATAR_CROP_QUALITY_FLOOR,
    _avatar_candidates_by_person,
    _cluster_avatar_attribution,
)


def _cluster(**overrides):
    row = {
        "id": "face_cluster_000001",
        "linked_person_group_id": "",
        "candidate_people": [],
        "face_track_ids": [],
        "face_observation_ids": [],
    }
    row.update(overrides)
    return row


def _track(track_id="track_a", top_frames=None, representative=None, quality=None):
    return {
        "id": track_id,
        "top_frames": top_frames or [],
        "representative": representative or {},
        "quality": quality or {},
    }


def _observation(observation_id="face_observation_000001", path="thumbs/obs.jpg", weight=0.9, bbox=None):
    return {
        "id": observation_id,
        "face_thumbnail_path": path,
        "face_quality_weight": weight,
        "bbox": bbox or {"width": 100, "height": 110},
    }


def test_linked_cluster_always_attributed_at_full_confidence():
    cluster = _cluster(linked_person_group_id="people_group_000001")
    assert _cluster_avatar_attribution(cluster) == ("people_group_000001", 1.0)


def test_tied_rival_candidates_produce_no_attribution():
    cluster = _cluster(
        candidate_people=[
            {"person_group_id": "people_group_000001", "confidence": 0.38, "person_label": "Lyudmila"},
            {"person_group_id": "people_group_000002", "confidence": 0.38, "person_label": "Zoya"},
        ]
    )
    assert _cluster_avatar_attribution(cluster) is None


def test_tie_between_duplicate_groups_of_same_person_is_not_ambiguous():
    cluster = _cluster(
        candidate_people=[
            {"person_group_id": "people_group_000005", "confidence": 0.7, "person_label": "Ekaterina / Katya"},
            {"person_group_id": "people_group_000020", "confidence": 0.68, "person_label": "Ekaterina / Katya / Катя"},
        ]
    )
    assert _cluster_avatar_attribution(cluster) == ("people_group_000005", 0.7)


def test_low_confidence_needs_direct_name_signal():
    co_occurrence_only = _cluster(
        candidate_people=[{"person_group_id": "people_group_000001", "confidence": 0.5}]
    )
    assert _cluster_avatar_attribution(co_occurrence_only) is None

    named_on_tape = _cluster(
        candidate_people=[
            {"person_group_id": "people_group_000001", "confidence": 0.5, "direct_name_strength": 0.9}
        ]
    )
    assert _cluster_avatar_attribution(named_on_tape) == ("people_group_000001", 0.5)


def test_track_frames_outrank_blurry_observations():
    cluster = _cluster(
        linked_person_group_id="people_group_000001",
        face_track_ids=["track_a"],
        face_observation_ids=["face_observation_000001"],
    )
    tracks = [
        _track(
            top_frames=[
                {"crop_path": "thumbs/track_k1.jpg", "quality_w": 0.98, "bbox": {"width": 100, "height": 105}}
            ]
        )
    ]
    observations = [_observation(weight=0.98)]

    candidates = _avatar_candidates_by_person([cluster], tracks, observations)["people_group_000001"]
    assert candidates[0]["path"] == "thumbs/track_k1.jpg"
    assert candidates[0]["source"] == "track_frame"
    # Same raw weight, but the keyframe-era observation carries the 0.85 source factor.
    assert candidates[1]["path"] == "thumbs/obs.jpg"
    assert candidates[1]["quality"] < candidates[0]["quality"]


def test_profile_aspect_is_penalized():
    cluster = _cluster(
        linked_person_group_id="people_group_000001",
        face_track_ids=["track_a"],
    )
    tracks = [
        _track(
            top_frames=[
                {"crop_path": "thumbs/frontal.jpg", "quality_w": 0.8, "bbox": {"width": 100, "height": 105}},
                {"crop_path": "thumbs/profile.jpg", "quality_w": 0.9, "bbox": {"width": 60, "height": 110}},
            ]
        )
    ]
    candidates = _avatar_candidates_by_person([cluster], tracks, [])["people_group_000001"]
    assert candidates[0]["path"] == "thumbs/frontal.jpg"


def test_crop_quality_floor_yields_no_candidates():
    cluster = _cluster(
        linked_person_group_id="people_group_000001",
        face_observation_ids=["face_observation_000001"],
    )
    observations = [_observation(weight=AVATAR_CROP_QUALITY_FLOOR / 2)]
    assert _avatar_candidates_by_person([cluster], [], observations) == {}


def test_top_three_by_attribution_times_quality():
    confirmed = _cluster(
        id="face_cluster_000001",
        linked_person_group_id="people_group_000001",
        face_observation_ids=["obs_confirmed"],
    )
    guessed = _cluster(
        id="face_cluster_000002",
        candidate_people=[{"person_group_id": "people_group_000001", "confidence": 0.7}],
        face_observation_ids=["obs_a", "obs_b", "obs_c"],
    )
    observations = [
        _observation("obs_confirmed", "thumbs/confirmed.jpg", weight=0.7),
        _observation("obs_a", "thumbs/a.jpg", weight=0.99),
        _observation("obs_b", "thumbs/b.jpg", weight=0.95),
        _observation("obs_c", "thumbs/c.jpg", weight=0.9),
    ]
    candidates = _avatar_candidates_by_person([confirmed, guessed], [], observations)["people_group_000001"]
    assert len(candidates) == 3
    # Confirmed cluster's decent crop beats the guessed cluster's sharper ones on score.
    assert candidates[0]["path"] == "thumbs/confirmed.jpg"
    assert [row["path"] for row in candidates[1:]] == ["thumbs/a.jpg", "thumbs/b.jpg"]

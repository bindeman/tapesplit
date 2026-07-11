from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
import re
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DEFAULT_MIN_SPEAKER_IDENTITY_CONFIDENCE = 0.28
DEFAULT_MAX_CANDIDATES_PER_SPEAKER = 5

# Anonymous diarization buckets (AZ_SPEAKER_*) are per-tape buckets, not
# people: measured cross-tape voiceprint self-consistency is 0.25-0.33 vs
# 0.52-0.64 for named speakers, so unifying them across tapes conflates
# different voices. Named labels stay archive-wide (reference-anchored).
ANONYMOUS_SPEAKER_PREFIX = "AZ_SPEAKER_"
VOICE_BUCKET_ANALYSIS_FILENAME = "voice_bucket_analysis.jsonl"
VOICE_BUCKET_STRONG_WEIGHT = 0.62
VOICE_BUCKET_AMBIGUOUS_WEIGHT = 0.40
VOICE_BUCKET_RECURRING_WEIGHT = 0.40

ROLE_ONLY_PEOPLE = {
    "adult",
    "adults",
    "boy",
    "boys",
    "child",
    "children",
    "dad",
    "daddy",
    "family",
    "father",
    "friend",
    "friends",
    "girl",
    "girls",
    "group",
    "host",
    "hostess",
    "kid",
    "kids",
    "mama",
    "mom",
    "mommy",
    "mother",
    "papa",
    "people",
    "person",
    "teacher",
    "teachers",
}


def build_speaker_identity_candidates(
    project_dir: Path,
    *,
    min_confidence: float = DEFAULT_MIN_SPEAKER_IDENTITY_CONFIDENCE,
    max_candidates_per_speaker: int = DEFAULT_MAX_CANDIDATES_PER_SPEAKER,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    speaker_segments = read_jsonl(project / "speaker_segments.jsonl")
    if not speaker_segments:
        raise FileNotFoundError("no speaker segments found; run `tapesplit speakers diarize` or import speaker segments first")

    events = read_jsonl(project / "canonical_events.jsonl")
    people = read_jsonl(project / "people_groups.jsonl")
    transcripts = read_jsonl(project / "transcript_segments.jsonl")
    relationships = read_jsonl(project / "relationship_candidates.jsonl")
    face_identity_candidates = read_jsonl(project / "face_identity_candidates.jsonl")
    face_clusters = read_jsonl(project / "face_clusters.jsonl")

    people_by_id = {str(row.get("id") or ""): row for row in people if row.get("id")}
    events_by_id = {str(row.get("id") or ""): row for row in events if row.get("id")}
    people_by_event = _people_by_event(people)
    event_intervals = _event_intervals_by_source(events)
    transcripts_by_id = {str(row.get("id") or ""): row for row in transcripts if row.get("id")}
    speaker_tracks = _speaker_tracks(speaker_segments, event_intervals, transcripts_by_id)
    event_speaker_durations = _event_speaker_durations(speaker_segments, event_intervals)
    role_identity_options = _role_identity_options_by_person(people, relationships, events_by_id)
    face_context = _face_candidate_context(face_identity_candidates, face_clusters)

    voice_bucket_analysis = _load_voice_bucket_analysis(project)

    rows: list[dict[str, Any]] = []
    for speaker_label, track in sorted(speaker_tracks.items()):
        buckets: dict[str, dict[str, Any]] = {}
        mentioned_people = _mentioned_people(track, people)
        _add_voice_bucket_candidates(
            buckets,
            track=track,
            analysis=voice_bucket_analysis,
            people=people,
        )
        _add_role_context_candidates(
            buckets,
            track=track,
            people_by_event=people_by_event,
            role_identity_options=role_identity_options,
            event_speaker_durations=event_speaker_durations,
        )
        _add_self_identification_candidates(buckets, track=track, people=people)
        _add_relationship_scope_candidates(
            buckets,
            track=track,
            relationships=relationships,
            people_by_id=people_by_id,
        )
        _add_face_context_candidates(
            buckets,
            track=track,
            face_context=face_context,
            people_by_id=people_by_id,
        )
        finalized = [
            _candidate_record(
                len(rows) + index,
                bucket,
                track=track,
                people_by_id=people_by_id,
                mentioned_people=mentioned_people,
            )
            for index, bucket in enumerate(buckets.values(), start=1)
        ]
        finalized = [
            row
            for row in finalized
            if row["confidence"] >= min_confidence
            and row.get("person_group_id")
            and row.get("speaker_label") == speaker_label
        ]
        finalized.sort(
            key=lambda row: (
                -float(row.get("confidence") or 0.0),
                str(row.get("person_label") or "").casefold(),
            )
        )
        rows.extend(finalized[:max_candidates_per_speaker])

    for index, row in enumerate(rows, start=1):
        row["id"] = f"speaker_identity_candidate_{index:06d}"
    output = project / "speaker_identity_candidates.jsonl"
    _write_jsonl(output, rows)

    voice_flags = _voice_bucket_flags(voice_bucket_analysis)
    if voice_flags:
        flags_path = project / "verification_flags.jsonl"
        existing_ids = {str(row.get("id") or "") for row in read_jsonl(flags_path)}
        for flag in voice_flags:
            if flag["id"] not in existing_ids:
                append_jsonl(flags_path, flag)

    return {
        "project": str(project),
        "speaker_tracks": len(speaker_tracks),
        "speaker_identity_candidates": len(rows),
        "voice_bucket_rows": len(voice_bucket_analysis),
        "voice_bucket_flags": len(voice_flags),
        "output": str(output),
        "by_speaker": _count_by(rows, "speaker_label"),
    }


def _speaker_tracks(
    speaker_segments: list[dict[str, Any]],
    event_intervals: dict[str, list[dict[str, Any]]],
    transcripts_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for segment in sorted(speaker_segments, key=lambda row: (str(row.get("source_video_id") or ""), _number_or_large(row.get("start_s")))):
        speaker_label = str(segment.get("speaker_label") or "").strip()
        if not speaker_label:
            continue
        if speaker_label.startswith(ANONYMOUS_SPEAKER_PREFIX):
            speaker_label = f"{speaker_label}@{segment.get('source_video_id')}"
        track = grouped.setdefault(
            speaker_label,
            {
                "speaker_label": speaker_label,
                "segments": [],
                "speaker_segment_ids": [],
                "source_video_ids": set(),
                "event_ids": set(),
                "transcript_ids": [],
                "texts": [],
                "source_ranges": [],
                "total_duration_s": 0.0,
            },
        )
        track["segments"].append(segment)
        if segment.get("id"):
            track["speaker_segment_ids"].append(str(segment["id"]))
        source_video_id = str(segment.get("source_video_id") or "")
        if source_video_id:
            track["source_video_ids"].add(source_video_id)
        start = _number_or_none(segment.get("start_s"))
        end = _number_or_none(segment.get("end_s"))
        if start is not None and end is not None:
            if end < start:
                start, end = end, start
            track["source_ranges"].append({"source_video_id": source_video_id, "start_s": start, "end_s": end})
            track["total_duration_s"] += max(0.0, end - start)
        for event in _events_for_segment(segment, event_intervals):
            if event.get("id"):
                track["event_ids"].add(str(event["id"]))
        for transcript_id in _segment_transcript_ids(segment):
            if transcript_id and transcript_id not in track["transcript_ids"]:
                track["transcript_ids"].append(transcript_id)
                transcript = transcripts_by_id.get(transcript_id)
                if transcript and transcript.get("text"):
                    track["texts"].append(str(transcript["text"]))
        metadata = segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {}
        metadata_text = str(metadata.get("transcript_text") or "").strip()
        if metadata_text and metadata_text not in track["texts"]:
            track["texts"].append(metadata_text)
    for track in grouped.values():
        track["source_video_ids"] = sorted(track["source_video_ids"])
        track["event_ids"] = sorted(track["event_ids"])
        track["first_start_s"] = min((_number_or_large(row.get("start_s")) for row in track["segments"]), default=None)
        track["last_end_s"] = max((_number_or_none(row.get("end_s")) or 0.0 for row in track["segments"]), default=None)
        track["total_duration_s"] = round(track["total_duration_s"], 3)
    return grouped


def _load_voice_bucket_analysis(project: Path) -> dict[str, dict[str, Any]]:
    """Analysis rows keyed by scoped anonymous label ('AZ_SPEAKER_00@video_000005')."""
    analysis: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(project / VOICE_BUCKET_ANALYSIS_FILENAME):
        if not isinstance(row, dict):
            continue
        key = f"{row.get('speaker_label')}@{row.get('tape')}"
        analysis[key] = row
    return analysis


def _person_by_alias(people: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Resolve a name to one person group, preferring alias-pure groups.

    Groups with accreted stray aliases (the known contamination blobs) match
    many names incidentally; the wanted name being a small minority of a
    group's alias parts is evidence the match is stray, not identity.
    """
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    for person in people:
        aliases = [str(person.get("label") or ""), *(str(a) for a in person.get("aliases") or [])]
        parts = [part.strip().casefold() for alias in aliases for part in alias.split("/") if part.strip()]
        unique_parts = set(parts)
        if wanted not in unique_parts:
            continue
        purity = sum(1 for part in parts if part == wanted) / max(1, len(parts))
        scored.append((purity, person))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    if len(scored) == 1:
        return scored[0][1]
    best, runner = scored[0], scored[1]
    if best[0] >= runner[0] + 0.15:
        return best[1]
    # Duplicate twin groups (same normalized label, e.g. two "Ekaterina / Katya"
    # rows re-split by a rebuild) are safe to resolve to the richer twin:
    # they are each other's top merge candidates and converge after merging.
    label_a = str(best[1].get("label") or "").strip().casefold()
    label_b = str(runner[1].get("label") or "").strip().casefold()
    if label_a and label_a == label_b:
        richer = max(
            (item[1] for item in scored if str(item[1].get("label") or "").strip().casefold() == label_a),
            key=lambda g: len(g.get("canonical_event_ids") or []),
        )
        return richer
    return None


def _add_voice_bucket_candidates(
    buckets: dict[str, dict[str, Any]],
    *,
    track: dict[str, Any],
    analysis: dict[str, dict[str, Any]],
    people: list[dict[str, Any]],
) -> None:
    row = analysis.get(str(track.get("speaker_label") or ""))
    if not row:
        return
    verdict = str(row.get("verdict") or "")
    if verdict == "named_overflow":
        person = _person_by_alias(people, str(row.get("match_label") or ""))
        if person is None:
            return
        ambiguous = bool(row.get("ambiguous"))
        weight = VOICE_BUCKET_AMBIGUOUS_WEIGHT if ambiguous else VOICE_BUCKET_STRONG_WEIGHT
        detail = (
            f"same-channel voiceprint cosine {row.get('match_cosine')} vs {row.get('match_label')}"
            + (f" (runner-up {row.get('second_cosine')} — ambiguous)" if ambiguous else "")
        )
        _add_signal(
            _bucket(buckets, track, person),
            weight=weight,
            signal="anonymous diarization bucket matches this person's voiceprint on the same tape",
            basis=f"{track['speaker_label']}: {detail}",
            source="voice_bucket_match",
        )
    elif verdict == "recurring_adult":
        # A recurring unnamed adult voice across family tapes: directive
        # register, rarely on screen — the camera-operator profile. Emitted
        # only as a low-confidence hypothesis; the review item carries the
        # full evidence and a human decides who this is.
        person = _person_by_alias(people, "Viktor Sokolov")
        if person is None:
            return
        _add_signal(
            _bucket(buckets, track, person),
            weight=VOICE_BUCKET_RECURRING_WEIGHT,
            signal="recurring unnamed adult voice (camera-operator profile) — identity hypothesis",
            basis=(
                f"{track['speaker_label']}: cross-tape voiceprint cohesion "
                f"{row.get('recurring_cluster_mean_cosine')} across the recurring-adult cluster; "
                f"on-screen {float(row.get('onscreen_fraction') or 0) * 100:.0f}% of speech; "
                f"directive register {float(row.get('directive_rate') or 0) * 100:.1f}%; "
                "hypothesis only — most-frequent unnamed adult in the archive; confirm in review"
            ),
            source="voice_bucket_recurring_adult",
        )


def _voice_bucket_flags(analysis: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for key, row in sorted(analysis.items()):
        verdict = str(row.get("verdict") or "")
        if verdict == "broadcast":
            flags.append(
                {
                    "id": f"vflag_voice_{row.get('tape')}_{row.get('speaker_label')}",
                    "verification_id": str(row.get("id") or ""),
                    "target_type": "speaker_track",
                    "target_id": key,
                    "claim_type": "speaker_identity",
                    "claim_text": f"{key} carries family speech",
                    "action": "mark_unrelated",
                    "reason": (
                        f"broadcast/TV audio profile: on-screen {float(row.get('onscreen_fraction') or 0) * 100:.0f}%, "
                        f"directive register {float(row.get('directive_rate') or 0) * 100:.1f}%, "
                        f"no voiceprint match to any named speaker ({row.get('segment_count')} segments, "
                        f"{row.get('duration_min')} min) — exclude from people, quotes, and journal sources"
                    ),
                    "source_video_id": str(row.get("tape") or ""),
                    "clip_path": None,
                }
            )
        elif verdict == "recurring_adult":
            flags.append(
                {
                    "id": f"vflag_voice_{row.get('tape')}_{row.get('speaker_label')}",
                    "verification_id": str(row.get("id") or ""),
                    "target_type": "speaker_track",
                    "target_id": key,
                    "claim_type": "speaker_identity",
                    "claim_text": f"{key} is an unidentified speaker",
                    "action": "confirm_speaker_identity",
                    "reason": (
                        "recurring unnamed adult across family tapes (camera-operator profile): "
                        f"cross-tape voiceprint cohesion {row.get('recurring_cluster_mean_cosine')}, "
                        f"on-screen {float(row.get('onscreen_fraction') or 0) * 100:.0f}%, "
                        f"directive register {float(row.get('directive_rate') or 0) * 100:.1f}% — "
                        "likely the person filming; needs a human name"
                    ),
                    "source_video_id": str(row.get("tape") or ""),
                    "clip_path": None,
                }
            )
    return flags


def _add_role_context_candidates(
    buckets: dict[str, dict[str, Any]],
    *,
    track: dict[str, Any],
    people_by_event: dict[str, list[dict[str, Any]]],
    role_identity_options: dict[str, list[dict[str, Any]]],
    event_speaker_durations: dict[str, dict[str, float]],
) -> None:
    for event_id in track["event_ids"]:
        for person in people_by_event.get(event_id, []):
            if str(person.get("kind") or "") != "role_candidate":
                continue
            role_confidence = _number_or_none(person.get("confidence")) or 0.55
            _add_signal(
                _bucket(buckets, track, person),
                weight=0.28 + min(role_confidence, 1.0) * 0.12,
                signal="speaker overlaps an event where this role is present",
                basis=f"{track['speaker_label']} speaks during an event that includes role {person.get('label')}",
                event_ids=[event_id],
                source="role_event_context",
            )
            role_share = _speaker_event_share(track["speaker_label"], event_id, event_speaker_durations)
            for option in role_identity_options.get(str(person.get("id") or ""), [])[:3]:
                if role_share["rank"] != 1 or role_share["share"] < 0.35:
                    continue
                named_person = {
                    "id": option.get("person_group_id"),
                    "label": option.get("label"),
                    "kind": "person_candidate",
                }
                _add_signal(
                    _bucket(buckets, track, named_person),
                    weight=0.34 + role_share["share"] * 0.18 + min(_number_or_none(option.get("confidence")) or 0.0, 1.0) * 0.14,
                    signal="role can be linked to a named person through relationship context",
                    basis=(
                        f"{track['speaker_label']} is the dominant voice during {person.get('label')}'s event and "
                        f"that role can be merged into {option.get('label')} from relationship evidence"
                    ),
                    event_ids=[event_id, *[str(item) for item in option.get("event_ids") or []]],
                    relationship_ids=[str(option.get("relationship_candidate_id") or "")],
                    source="role_identity_bridge",
                    metadata={
                        "role_person_group_id": person.get("id"),
                        "role_label": person.get("label"),
                        "role_identity_option": option,
                        "role_event_speaker_share": role_share,
                    },
                )


def _add_self_identification_candidates(
    buckets: dict[str, dict[str, Any]],
    *,
    track: dict[str, Any],
    people: list[dict[str, Any]],
) -> None:
    for text in track.get("texts") or []:
        for person in people:
            if not _identity_eligible_person(person):
                continue
            if not _self_identifies_person(text, person):
                continue
            _add_signal(
                _bucket(buckets, track, person),
                weight=0.72,
                signal="speaker appears to self-identify by name",
                basis=f"Transcript near {track['speaker_label']} contains a self-identification phrase for {person.get('label')}",
                transcript_ids=track.get("transcript_ids") or [],
                text_examples=[text],
                source="self_identification_phrase",
            )


def _add_relationship_scope_candidates(
    buckets: dict[str, dict[str, Any]],
    *,
    track: dict[str, Any],
    relationships: list[dict[str, Any]],
    people_by_id: dict[str, dict[str, Any]],
) -> None:
    for relationship in relationships:
        subject_id = str(relationship.get("subject_entity_id") or "")
        subject = people_by_id.get(subject_id)
        if not subject or not _identity_eligible_person(subject):
            continue
        if not _track_overlaps_scope(track, relationship.get("scope") if isinstance(relationship.get("scope"), dict) else {}):
            continue
        _add_signal(
            _bucket(buckets, track, subject),
            weight=0.18 + min(_number_or_none(relationship.get("confidence")) or 0.0, 1.0) * 0.07,
            signal="speaker overlaps a relationship observation naming this person",
            basis=f"{track['speaker_label']} overlaps relationship evidence for {relationship.get('subject_label')}",
            event_ids=[str(item) for item in (relationship.get("scope") or {}).get("canonical_event_ids") or []],
            relationship_ids=[str(relationship.get("id") or "")],
            transcript_ids=[str(item) for item in (relationship.get("metadata") or {}).get("transcript_segment_ids") or []],
            source="named_relationship_scope_overlap",
        )


def _add_face_context_candidates(
    buckets: dict[str, dict[str, Any]],
    *,
    track: dict[str, Any],
    face_context: dict[str, list[dict[str, Any]]],
    people_by_id: dict[str, dict[str, Any]],
) -> None:
    event_ids = set(track.get("event_ids") or [])
    if not event_ids:
        return
    for person_id, candidates in face_context.items():
        person = people_by_id.get(person_id)
        if not person or not _identity_eligible_person(person):
            continue
        support = [
            row for row in candidates if event_ids.intersection({str(item) for item in row.get("supporting_event_ids") or []})
        ]
        if not support:
            continue
        best = max(support, key=lambda row: _number_or_none(row.get("confidence")) or 0.0)
        review_status = str(best.get("review_status") or "")
        weight = 0.52 if review_status == "confirmed" else 0.18
        weight += min(_number_or_none(best.get("confidence")) or 0.0, 1.0) * 0.12
        _add_signal(
            _bucket(buckets, track, person),
            weight=weight,
            signal="speaker overlaps events with a face candidate for this person",
            basis=f"Face identity candidate for {person.get('label')} appears in events where {track['speaker_label']} speaks",
            event_ids=[str(item) for row in support for item in row.get("supporting_event_ids") or []],
            face_cluster_ids=[str(row.get("face_cluster_id") or "") for row in support],
            source="face_event_context",
        )


def _candidate_record(
    index: int,
    bucket: dict[str, Any],
    *,
    track: dict[str, Any],
    people_by_id: dict[str, dict[str, Any]],
    mentioned_people: list[dict[str, Any]],
) -> dict[str, Any]:
    person = people_by_id.get(str(bucket.get("person_group_id") or "")) or {}
    signals = bucket.get("signals") or []
    weights = [float(signal.get("weight") or 0.0) for signal in signals]
    max_weight = max(weights, default=0.0)
    support_bonus = min(0.18, sum(weights) * 0.12)
    event_bonus = min(0.08, len(bucket.get("canonical_event_ids") or []) * 0.02)
    segment_bonus = min(0.08, len(bucket.get("speaker_segment_ids") or []) / 20.0 * 0.08)
    confidence = max_weight + support_bonus + event_bonus + segment_bonus
    if not any(signal.get("source") in {"self_identification_phrase", "role_identity_bridge"} for signal in signals):
        confidence = min(confidence, 0.72)
    sources = {str(signal.get("source") or "") for signal in signals}
    if sources and all(source.startswith("voice_bucket") for source in sources):
        # Voiceprint alone is one dimension — keep single-signal buckets below
        # the confirm_speaker_identity auto-accept floor; a human confirms.
        confidence = min(confidence, 0.68)
    if str(person.get("kind") or "") == "role_candidate":
        confidence = min(confidence, 0.78)
    confidence = max(0.05, min(0.94, confidence))
    return {
        "id": f"speaker_identity_candidate_{index:06d}",
        "speaker_label": bucket["speaker_label"],
        "person_group_id": bucket["person_group_id"],
        "person_label": bucket.get("person_label") or person.get("label") or bucket["person_group_id"],
        "person_kind": person.get("kind") or bucket.get("person_kind") or "person_candidate",
        "confidence": round(confidence, 3),
        "review_status": "needs_review" if confidence >= 0.5 else "unreviewed",
        "source": "local_speaker_identity_context",
        "source_video_ids": track.get("source_video_ids") or [],
        "speaker_segment_ids": _unique_items(bucket.get("speaker_segment_ids") or []),
        "transcript_segment_ids": _unique_items(bucket.get("transcript_segment_ids") or []),
        "canonical_event_ids": _unique_items(bucket.get("canonical_event_ids") or []),
        "relationship_candidate_ids": _unique_items(bucket.get("relationship_candidate_ids") or []),
        "face_cluster_ids": _unique_items(bucket.get("face_cluster_ids") or []),
        "first_start_s": track.get("first_start_s"),
        "last_end_s": track.get("last_end_s"),
        "total_duration_s": track.get("total_duration_s"),
        "scope": {
            "source_video_ids": track.get("source_video_ids") or [],
            "source_ranges": track.get("source_ranges") or [],
            "canonical_event_ids": _unique_items(bucket.get("canonical_event_ids") or []),
        },
        "supporting_signals": _unique_items(signal.get("signal") for signal in signals if signal.get("signal")),
        "basis": _unique_items(signal.get("basis") for signal in signals if signal.get("basis")),
        "text_examples": _unique_items(bucket.get("text_examples") or [])[:5],
        "mentioned_people": mentioned_people[:8],
        "metadata": {
            "signal_sources": _count_by(signals, "source"),
            "signals": signals,
            **bucket.get("metadata", {}),
        },
    }


def _bucket(buckets: dict[str, dict[str, Any]], track: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
    person_id = str(person.get("id") or person.get("person_group_id") or "")
    bucket = buckets.setdefault(
        person_id,
        {
            "speaker_label": track["speaker_label"],
            "person_group_id": person_id,
            "person_label": str(person.get("label") or person.get("person_label") or person_id),
            "person_kind": person.get("kind"),
            "speaker_segment_ids": [],
            "transcript_segment_ids": [],
            "canonical_event_ids": [],
            "relationship_candidate_ids": [],
            "face_cluster_ids": [],
            "text_examples": [],
            "signals": [],
            "metadata": {},
        },
    )
    for segment_id in track.get("speaker_segment_ids") or []:
        _append_unique(bucket, "speaker_segment_ids", segment_id)
    for transcript_id in track.get("transcript_ids") or []:
        _append_unique(bucket, "transcript_segment_ids", transcript_id)
    return bucket


def _add_signal(
    bucket: dict[str, Any],
    *,
    weight: float,
    signal: str,
    basis: str,
    source: str,
    event_ids: list[str] | None = None,
    relationship_ids: list[str] | None = None,
    face_cluster_ids: list[str] | None = None,
    transcript_ids: list[str] | None = None,
    text_examples: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    for event_id in event_ids or []:
        _append_unique(bucket, "canonical_event_ids", event_id)
    for relationship_id in relationship_ids or []:
        _append_unique(bucket, "relationship_candidate_ids", relationship_id)
    for face_cluster_id in face_cluster_ids or []:
        _append_unique(bucket, "face_cluster_ids", face_cluster_id)
    for transcript_id in transcript_ids or []:
        _append_unique(bucket, "transcript_segment_ids", transcript_id)
    for text in text_examples or []:
        _append_unique(bucket, "text_examples", str(text).strip())
    bucket["signals"].append(
        {
            "source": source,
            "weight": round(max(0.0, min(weight, 1.0)), 3),
            "signal": signal,
            "basis": basis,
            "event_ids": _unique_items(event_ids or []),
            "relationship_candidate_ids": _unique_items(relationship_ids or []),
            "face_cluster_ids": _unique_items(face_cluster_ids or []),
            "transcript_segment_ids": _unique_items(transcript_ids or []),
        }
    )
    if metadata:
        bucket["metadata"].update(metadata)


def _mentioned_people(track: dict[str, Any], people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = []
    texts = [str(text or "") for text in track.get("texts") or []]
    for person in people:
        if not _identity_eligible_person(person):
            continue
        count = sum(1 for text in texts if _person_terms_in_text(person, text))
        if count:
            counts.append(
                {
                    "person_group_id": str(person.get("id") or ""),
                    "person_label": str(person.get("label") or ""),
                    "mention_count": count,
                }
            )
    return sorted(counts, key=lambda row: (-int(row["mention_count"]), row["person_label"].casefold()))


def _role_identity_options_by_person(
    people: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    people_by_id = {str(person.get("id") or ""): person for person in people if person.get("id")}
    person_id_by_alias = _person_id_by_alias(people)
    named_relationships: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for relationship in relationships:
        predicate = str(relationship.get("predicate") or "")
        subject_id = str(relationship.get("subject_entity_id") or "")
        object_id = str(relationship.get("object_entity_id") or "")
        subject = people_by_id.get(subject_id)
        if not subject or str(subject.get("kind") or "") == "role_candidate" or not object_id:
            continue
        named_relationships[(predicate, object_id)].append(relationship)

    options_by_person: dict[str, list[dict[str, Any]]] = {}
    for person in people:
        person_id = str(person.get("id") or "")
        predicate = _role_person_predicate(person)
        if not person_id or not predicate:
            continue
        options = []
        for object_id in _role_event_object_person_ids(person, events_by_id, person_id_by_alias, exclude_id=person_id):
            for relationship in named_relationships.get((predicate, object_id), []):
                subject_id = str(relationship.get("subject_entity_id") or "")
                subject = people_by_id.get(subject_id)
                if not subject:
                    continue
                event_ids = [str(item) for item in (relationship.get("scope") or {}).get("canonical_event_ids") or []]
                confidence = min(0.95, (_number_or_none(relationship.get("confidence")) or 0.7) + 0.04)
                options.append(
                    {
                        "person_group_id": subject_id,
                        "label": subject.get("label"),
                        "confidence": round(confidence, 3),
                        "predicate": predicate,
                        "object_person_group_id": object_id,
                        "object_label": (people_by_id.get(object_id) or {}).get("label") or relationship.get("object_label"),
                        "relationship_candidate_id": relationship.get("id"),
                        "event_ids": event_ids,
                        "basis": [
                            f"{relationship.get('subject_label')} is a {predicate.replace('_candidate', '').replace('_', ' ')} of {relationship.get('object_label')}",
                            f"{person.get('label')} appears as a role in event(s) with {(people_by_id.get(object_id) or {}).get('label') or object_id}",
                        ],
                    }
                )
        if options:
            options_by_person[person_id] = _dedupe_role_identity_options(options)
    return options_by_person


def _face_candidate_context(
    face_identity_candidates: list[dict[str, Any]],
    face_clusters: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    clusters_by_id = {str(row.get("id") or ""): row for row in face_clusters if row.get("id")}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in face_identity_candidates:
        person_id = str(candidate.get("person_group_id") or "")
        cluster_id = str(candidate.get("face_cluster_id") or "")
        if not person_id:
            continue
        cluster = clusters_by_id.get(cluster_id) or {}
        grouped[person_id].append(
            {
                **candidate,
                "thumbnail_path": cluster.get("thumbnail_path"),
                "cluster_review_status": cluster.get("review_status"),
                "linked_person_group_id": cluster.get("linked_person_group_id"),
            }
        )
    return dict(grouped)


def _event_intervals_by_source(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        for interval in _event_intervals(event):
            by_source[interval["source_video_id"]].append({"event": event, **interval})
    return dict(by_source)


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


def _events_for_segment(segment: dict[str, Any], event_intervals: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    source_video_id = str(segment.get("source_video_id") or "")
    start = _number_or_none(segment.get("start_s"))
    end = _number_or_none(segment.get("end_s"))
    if not source_video_id or start is None:
        return []
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    matches = []
    seen = set()
    for interval in event_intervals.get(source_video_id, []):
        if _ranges_overlap(start, end, float(interval["start_s"]), float(interval["end_s"])):
            event = interval["event"]
            event_id = str(event.get("id") or "")
            if event_id and event_id not in seen:
                matches.append(event)
                seen.add(event_id)
    return matches


def _event_speaker_durations(
    speaker_segments: list[dict[str, Any]],
    event_intervals: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    durations: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for segment in speaker_segments:
        speaker_label = str(segment.get("speaker_label") or "")
        source_video_id = str(segment.get("source_video_id") or "")
        start = _number_or_none(segment.get("start_s"))
        end = _number_or_none(segment.get("end_s"))
        if not speaker_label or not source_video_id or start is None:
            continue
        if end is None:
            end = start
        if end < start:
            start, end = end, start
        for interval in event_intervals.get(source_video_id, []):
            overlap_start = max(start, float(interval["start_s"]))
            overlap_end = min(end, float(interval["end_s"]))
            if overlap_end <= overlap_start:
                continue
            event = interval["event"]
            event_id = str(event.get("id") or "")
            if event_id:
                durations[event_id][speaker_label] += overlap_end - overlap_start
    return {event_id: dict(values) for event_id, values in durations.items()}


def _speaker_event_share(
    speaker_label: str,
    event_id: str,
    event_speaker_durations: dict[str, dict[str, float]],
) -> dict[str, Any]:
    durations = event_speaker_durations.get(event_id) or {}
    total = sum(durations.values()) or 1.0
    sorted_durations = sorted(durations.items(), key=lambda item: (-item[1], item[0]))
    rank = next((index for index, item in enumerate(sorted_durations, start=1) if item[0] == speaker_label), 999)
    speaker_duration = durations.get(speaker_label, 0.0)
    return {
        "event_id": event_id,
        "speaker_duration_s": round(speaker_duration, 3),
        "total_speaker_duration_s": round(total, 3),
        "share": round(speaker_duration / total, 3),
        "rank": rank,
    }


def _track_overlaps_scope(track: dict[str, Any], scope: dict[str, Any]) -> bool:
    source_video_ids = {str(item) for item in scope.get("source_video_ids") or [] if item}
    start = _number_or_none(scope.get("start_s"))
    end = _number_or_none(scope.get("end_s"))
    event_ids = {str(item) for item in scope.get("canonical_event_ids") or [] if item}
    if start is None and end is None:
        return bool(event_ids and event_ids.intersection(set(track.get("event_ids") or [])))
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None:
        return False
    if end < start:
        start, end = end, start
    for source_range in track.get("source_ranges") or []:
        source_video_id = str(source_range.get("source_video_id") or "")
        if source_video_ids and source_video_id not in source_video_ids:
            continue
        range_start = _number_or_none(source_range.get("start_s"))
        range_end = _number_or_none(source_range.get("end_s"))
        if range_start is None:
            continue
        if range_end is None:
            range_end = range_start
        if _ranges_overlap(start, end, range_start, range_end):
            return True
    return False


def _people_by_event(people: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for person in people:
        for event_id in person.get("canonical_event_ids") or []:
            grouped[str(event_id)].append(person)
    return dict(grouped)


def _person_id_by_alias(people: list[dict[str, Any]]) -> dict[str, str]:
    lookup = {}
    for person in people:
        person_id = str(person.get("id") or "")
        labels = [person.get("label"), *(person.get("aliases") or [])]
        for label in labels:
            key = _normalize_context_text(label)
            if key and person_id:
                lookup[key] = person_id
            for part in str(label or "").replace("/", " ").replace("(", " ").replace(")", " ").split():
                part_key = _normalize_context_text(part)
                if part_key and person_id:
                    lookup.setdefault(part_key, person_id)
    return lookup


def _role_person_predicate(person: dict[str, Any]) -> str:
    if str(person.get("kind") or "") != "role_candidate":
        return ""
    role_text = " ".join([str(person.get("label") or ""), *[str(alias) for alias in person.get("aliases") or []]]).casefold()
    if any(term in role_text for term in ["mom", "mother", "mama", "мама", "маму", "маме"]):
        return "mother_candidate"
    if any(term in role_text for term in ["dad", "father", "papa", "папа", "папу", "папе"]):
        return "father_candidate"
    return ""


def _role_event_object_person_ids(
    person: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    person_id_by_alias: dict[str, str],
    *,
    exclude_id: str,
) -> list[str]:
    object_ids = []
    for event_id in person.get("canonical_event_ids") or []:
        event = events_by_id.get(str(event_id)) or {}
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        for label in metadata.get("people") or []:
            object_id = person_id_by_alias.get(_normalize_context_text(label))
            if object_id and object_id != exclude_id:
                object_ids.append(object_id)
    return _unique_items(object_ids)


def _dedupe_role_identity_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for option in options:
        key = str(option.get("person_group_id") or "")
        current = buckets.get(key)
        if current is None or (_number_or_none(option.get("confidence")) or 0.0) > (_number_or_none(current.get("confidence")) or 0.0):
            buckets[key] = option
    return sorted(buckets.values(), key=lambda row: -(_number_or_none(row.get("confidence")) or 0.0))


def _identity_eligible_person(person: dict[str, Any]) -> bool:
    label = str(person.get("label") or "")
    kind = str(person.get("kind") or "person_candidate")
    if kind in {"fictional_person", "group_candidate"}:
        return False
    normalized = _normalize_context_text(label)
    if normalized in ROLE_ONLY_PEOPLE and kind != "role_candidate":
        return False
    return bool(person.get("id") and label)


def _person_terms_in_text(person: dict[str, Any], value: Any) -> bool:
    normalized_text = _normalize_context_text(value)
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
        normalized = _normalize_context_text(label)
        if not normalized:
            continue
        if "/" not in str(label):
            terms.add(normalized)
        for part in normalized.split():
            if len(part) > 2 and part not in ROLE_ONLY_PEOPLE:
                terms.add(part)
    return terms


def _self_identifies_person(text: str, person: dict[str, Any]) -> bool:
    normalized = _normalize_context_text(text)
    if not normalized:
        return False
    for term in _person_name_terms(person):
        escaped = re.escape(term)
        patterns = [
            rf"\bi am {escaped}\b",
            rf"\bi m {escaped}\b",
            rf"\bmy name is {escaped}\b",
            rf"\bthis is {escaped}\b",
            rf"\bменя зовут {escaped}\b",
            rf"\bэто {escaped}\b",
        ]
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns):
            return True
    return False


def _segment_transcript_ids(segment: dict[str, Any]) -> list[str]:
    metadata = segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {}
    values = []
    if metadata.get("transcript_segment_id"):
        values.append(str(metadata["transcript_segment_id"]))
    for value in metadata.get("transcript_segment_ids") or []:
        if value:
            values.append(str(value))
    return _unique_items(values)


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


def _ranges_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> bool:
    return start_a <= end_b and start_b <= end_a


def _normalize_context_text(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^0-9a-zа-яё]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _append_unique(row: dict[str, Any], key: str, value: Any) -> None:
    if value in (None, ""):
        return
    values = row.get(key)
    if not isinstance(values, list):
        values = [values] if values not in (None, "") else []
        row[key] = values
    if value not in values:
        values.append(value)


def _unique_items(values: Any) -> list[Any]:
    seen = set()
    result = []
    for value in values or []:
        if value in (None, ""):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


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


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _number_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_or_large(value: Any) -> float:
    return _number_or_none(value) if _number_or_none(value) is not None else math.inf


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)
    if not rows:
        path.write_text("", encoding="utf-8")

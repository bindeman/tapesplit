"""Closed-loop claim verification: sample claims, clip footage, blind-verify.

Every pipeline claim — an event's content, a place, a date, a person's
presence — is an assertion tied to a time range on a source tape. This module
exploits a generator/verifier asymmetry: producing claims over hours of
footage is hard (long context, chunking, drift), but checking one claim
against a short clip is nearly trivial for a multimodal model.

The protocol is deliberately blind. The verifier backend NEVER sees the
claim; it only describes the clip (setting, activities, people count, visible
text, language, audio). A separate adjudicator compares that description to
the claim and issues SUPPORTED / CONTRADICTED / UNDECIDABLE. Asking "does
this clip show a geology lab?" invites confirmation; asking "describe this
clip" does not.

Verdicts land in ``verifications.jsonl`` and flow into the calibration loop
(``calibration.py``) as a machine reviewer (``clip-verifier``) at reduced
weight, so grounded precision tunes the safe auto-accept floors with far more
observations than human review alone provides — while human-only precision
stays separately reported. Contradictions are never destructive: they are
flagged to ``verification_flags.jsonl`` for review.
"""

from __future__ import annotations

import json
import os
import re
import random
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from tapesplit.storage import append_jsonl, read_jsonl

VERIFICATIONS_FILENAME = "verifications.jsonl"
VERIFICATION_FLAGS_FILENAME = "verification_flags.jsonl"
CLIPS_DIRNAME = "verification_clips"

# The reviewer identity machine verdicts carry into calibration.
MACHINE_REVIEWER = "clip-verifier"

DEFAULT_SAMPLE_SIZE = 40
DEFAULT_VERIFIER_DEPLOYMENT = "gpt-5.6-sol"
DEFAULT_ADJUDICATOR_DEPLOYMENT = "gpt-5.6-terra"

CLIP_PADDING_S = 5.0
CLIP_MAX_DURATION_S = 120.0
CLIP_HEIGHT = 480

# Frame-grid request shape (sol has no video input; probe-verified).
FRAME_HEIGHT = 360
FRAME_JPEG_QUALITY = 7
FRAME_MAX_COUNT = 40
FRAME_MIN_INTERVAL_S = 2.0
CONTENT_FILTER_MAX_PROBES = 8

# Oversampling triggers.
LOW_CONFIDENCE_BELOW = 0.6
CHUNK_BOUNDARY_WINDOW_S = 30.0
HIGH_BLAST_RADIUS_MIN_EVENTS = 10

# Baseline adjudicator thresholds.
SUPPORT_MIN_OVERLAP = 0.3
CONTRADICT_MIN_DESCRIPTION_TOKENS = 6

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_UNDECIDABLE = "UNDECIDABLE"

# Claim types and the suggestion action whose precision they inform.
CLAIM_ACTIONS = {
    "event_content": "confirm_event",
    "event_place": "confirm_place",
    "event_date": "confirm_event_date",
    "person_presence": "confirm_identity",
}

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_YEAR_RE = re.compile(r"\b(18[89]\d|19\d\d|20\d\d)\b")

_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in is it its of on or that the
    their there this to was were with while during into over about after before
    someone something people person shows showing appears""".split()
)


class VerifierNotConfigured(RuntimeError):
    """The verifier backend is missing config or explicit opt-in."""


# ---------------------------------------------------------------------------
# Claims


@dataclass(frozen=True)
class Claim:
    id: str
    claim_type: str
    action: str
    source_video_id: str
    start_s: float
    end_s: float
    text: str
    keywords: tuple[str, ...]
    target_id: str
    target_type: str
    confidence: float | None = None
    weight_reasons: tuple[str, ...] = ()

    @property
    def weight(self) -> float:
        return 1.0 + len(self.weight_reasons)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.casefold()) if t not in _STOPWORDS and len(t) > 2]


def _keywords(*parts: Any) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for part in parts:
        if part is None:
            continue
        values = part if isinstance(part, (list, tuple)) else [part]
        for value in values:
            for token in _tokens(str(value)):
                seen.setdefault(token, None)
    return tuple(seen)


def _primary_range(event: dict[str, Any]) -> tuple[str, float, float] | None:
    metadata = event.get("metadata") or {}
    ranges = metadata.get("source_ranges") or []
    best: tuple[str, float, float] | None = None
    for row in ranges:
        video = str(row.get("source_video_id") or "")
        try:
            start = float(row.get("start_s"))
            end = float(row.get("end_s"))
        except (TypeError, ValueError):
            continue
        if not video or end <= start:
            continue
        if best is None or (end - start) > (best[2] - best[1]):
            best = (video, start, end)
    return best


def _chunk_boundary_events(gemini_events: list[dict[str, Any]]) -> set[str]:
    """Gemini event ids that start or end near a chunk seam (offset-bug prone)."""

    boundary: set[str] = set()
    for row in gemini_events:
        metadata = row.get("metadata") or {}
        chunk_start = metadata.get("chunk_start_s")
        chunk_end = metadata.get("chunk_end_s")
        if chunk_start is None or chunk_end is None:
            continue
        index = metadata.get("chunk_index")
        try:
            start = float(row.get("start_s") or 0.0)
            end = float(row.get("end_s") or 0.0)
        except (TypeError, ValueError):
            continue
        near_seam = (
            abs(start - float(chunk_start)) <= CHUNK_BOUNDARY_WINDOW_S
            or abs(float(chunk_end) - end) <= CHUNK_BOUNDARY_WINDOW_S
        )
        if near_seam or (isinstance(index, int) and index >= 2):
            boundary.add(str(row.get("id") or ""))
    boundary.discard("")
    return boundary


def _auto_accepted_targets(project: Path) -> set[str]:
    from tapesplit.calibration import AUTO_REVIEWERS

    targets: set[str] = set()
    for row in read_jsonl(project / "corrections.jsonl"):
        if str(row.get("reviewer") or "") in AUTO_REVIEWERS and row.get("target_id"):
            targets.add(str(row["target_id"]))
    return targets


def enumerate_claims(project_dir: Path) -> list[Claim]:
    """All checkable claims the project's artifacts assert, with oversampling tags."""

    project = project_dir.expanduser().resolve()
    events = read_jsonl(project / "canonical_events.jsonl")
    gemini_events = read_jsonl(project / "gemini_events.jsonl")
    place_roles = read_jsonl(project / "event_place_roles.jsonl")
    identity_candidates = read_jsonl(project / "face_identity_candidates.jsonl")

    boundary_gemini_ids = _chunk_boundary_events(gemini_events)
    auto_targets = _auto_accepted_targets(project)

    events_by_id = {str(row.get("id") or ""): row for row in events}
    place_event_counts: dict[str, int] = {}
    for row in place_roles:
        label = str(((row.get("candidate_options") or [{}])[0]).get("label") or row.get("label") or "")
        if label:
            place_event_counts[label] = place_event_counts.get(label, 0) + 1

    claims: list[Claim] = []
    counter = 0

    def make(
        claim_type: str,
        *,
        video: str,
        start: float,
        end: float,
        text: str,
        keywords: tuple[str, ...],
        target_id: str,
        target_type: str,
        confidence: float | None,
        reasons: list[str],
    ) -> None:
        nonlocal counter
        counter += 1
        if confidence is not None and confidence < LOW_CONFIDENCE_BELOW:
            reasons.append("low-confidence")
        if target_id in auto_targets:
            reasons.append("auto-accepted")
        claims.append(
            Claim(
                id=f"claim_{counter:06d}",
                claim_type=claim_type,
                action=CLAIM_ACTIONS[claim_type],
                source_video_id=video,
                start_s=start,
                end_s=end,
                text=text,
                keywords=keywords,
                target_id=target_id,
                target_type=target_type,
                confidence=confidence,
                weight_reasons=tuple(reasons),
            )
        )

    for event in events:
        event_id = str(event.get("id") or "")
        placement = _primary_range(event)
        if not event_id or placement is None:
            continue
        video, start, end = placement
        metadata = event.get("metadata") or {}
        source_event_ids = [str(x) for x in metadata.get("source_event_ids") or []]
        reasons = ["chunk-boundary"] if any(x in boundary_gemini_ids for x in source_event_ids) else []
        title = str(event.get("title") or "").strip()
        summary = str(event.get("summary") or "").strip()
        try:
            confidence = float(event.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        make(
            "event_content",
            video=video,
            start=start,
            end=end,
            text=" — ".join(part for part in (title, summary) if part),
            keywords=_keywords(title, summary, metadata.get("people"), metadata.get("place_candidates")),
            target_id=event_id,
            target_type="canonical_event",
            confidence=confidence,
            reasons=list(reasons),
        )
        for candidate in metadata.get("date_candidates") or []:
            match = _YEAR_RE.search(str(candidate))
            if not match:
                continue
            year = match.group(0)
            make(
                "event_date",
                video=video,
                start=start,
                end=end,
                text=f"This footage was captured in {year}.",
                keywords=(year,),
                target_id=event_id,
                target_type="canonical_event",
                confidence=confidence,
                reasons=list(reasons),
            )
            break  # one date claim per event keeps the pool balanced

    for row in place_roles:
        event_id = str(row.get("canonical_event_id") or "")
        event = events_by_id.get(event_id)
        placement = _primary_range(event) if event else None
        if placement is None:
            continue
        selected = next(
            (c for c in row.get("candidate_options") or [] if c.get("selected")),
            None,
        )
        if not selected:
            continue
        label = str(selected.get("label") or "").strip()
        role = str(selected.get("role") or "")
        if not label or role != "visible_place":
            continue
        video, start, end = placement
        try:
            confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        reasons = []
        if place_event_counts.get(label, 0) >= HIGH_BLAST_RADIUS_MIN_EVENTS:
            reasons.append("high-blast-radius")
        make(
            "event_place",
            video=video,
            start=start,
            end=end,
            text=f"The footage shows this place: {label}.",
            keywords=_keywords(label),
            target_id=str(row.get("id") or event_id),
            target_type="event_place_role",
            confidence=confidence,
            reasons=reasons,
        )

    for row in identity_candidates:
        label = str(row.get("person_label") or "").strip()
        candidate_id = str(row.get("id") or "")
        if not label or not candidate_id:
            continue
        event = None
        for event_id in row.get("supporting_event_ids") or []:
            event = events_by_id.get(str(event_id))
            if event and _primary_range(event):
                break
            event = None
        placement = _primary_range(event) if event else None
        if placement is None:
            continue
        video, start, end = placement
        try:
            confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        make(
            "person_presence",
            video=video,
            start=start,
            end=end,
            text=f"{label} appears in this footage.",
            keywords=_keywords(label),
            target_id=candidate_id,
            target_type="face_identity_candidate",
            confidence=confidence,
            reasons=[],
        )

    return claims


def sample_claims(
    claims: list[Claim],
    *,
    sample_size: int,
    seed: int = 0,
    types: tuple[str, ...] | None = None,
) -> list[Claim]:
    """Stratified (tape × claim type) weighted sample, deterministic per seed.

    Within each stratum claims are ordered by Efraimidis–Spirakis priority
    (weighted sampling without replacement), then strata are drained
    round-robin so every tape and claim type is represented before any
    stratum contributes twice.
    """

    pool = [c for c in claims if types is None or c.claim_type in types]
    strata: dict[tuple[str, str], list[Claim]] = {}
    for claim in pool:
        strata.setdefault((claim.source_video_id, claim.claim_type), []).append(claim)

    rng = random.Random(seed)
    ordered: list[list[Claim]] = []
    for key in sorted(strata):
        rows = sorted(strata[key], key=lambda c: c.id)
        keyed = [(rng.random() ** (1.0 / claim.weight), claim) for claim in rows]
        keyed.sort(key=lambda pair: -pair[0])
        ordered.append([claim for _, claim in keyed])

    sampled: list[Claim] = []
    while ordered and len(sampled) < sample_size:
        for stratum in list(ordered):
            if len(sampled) >= sample_size:
                break
            sampled.append(stratum.pop(0))
            if not stratum:
                ordered.remove(stratum)
    return sampled


# ---------------------------------------------------------------------------
# Clips


@dataclass(frozen=True)
class ClipPlan:
    claim: Claim
    source_path: Path | None
    clip_path: Path
    clip_start_s: float
    clip_end_s: float


def _resolve_source_path(project: Path, tape: dict[str, Any]) -> Path | None:
    path = tape.get("path")
    if path and Path(path).exists():
        return Path(path)
    relative = tape.get("relative_path")
    if relative:
        for base in (project.parent, project):
            candidate = base / str(relative)
            if candidate.exists():
                return candidate
    return None


def plan_clips(project_dir: Path, sampled: list[Claim]) -> list[ClipPlan]:
    project = project_dir.expanduser().resolve()
    tapes = {str(row.get("id") or ""): row for row in read_jsonl(project / "tapes.jsonl")}

    plans: list[ClipPlan] = []
    for claim in sampled:
        start = max(0.0, claim.start_s - CLIP_PADDING_S)
        end = claim.end_s + CLIP_PADDING_S
        if end - start > CLIP_MAX_DURATION_S:
            center = (claim.start_s + claim.end_s) / 2.0
            start = max(0.0, center - CLIP_MAX_DURATION_S / 2.0)
            end = start + CLIP_MAX_DURATION_S
        tape = tapes.get(claim.source_video_id) or {}
        clip_path = project / CLIPS_DIRNAME / claim.source_video_id / f"{claim.id}.mp4"
        plans.append(
            ClipPlan(
                claim=claim,
                source_path=_resolve_source_path(project, tape),
                clip_path=clip_path,
                clip_start_s=round(start, 3),
                clip_end_s=round(end, 3),
            )
        )
    return plans


def extract_clip(plan: ClipPlan, *, force: bool = False) -> Path:
    if plan.source_path is None:
        raise RuntimeError(f"source video for {plan.claim.source_video_id} not found on disk")
    if plan.clip_path.exists() and not force:
        return plan.clip_path
    plan.clip_path.parent.mkdir(parents=True, exist_ok=True)
    duration = plan.clip_end_s - plan.clip_start_s
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{plan.clip_start_s:.3f}",
        "-i",
        str(plan.source_path),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        f"scale=-2:{CLIP_HEIGHT}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-avoid_negative_ts",
        "make_zero",
        str(plan.clip_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for verification clips") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed extracting {plan.clip_path.name}: {exc.stderr[-400:]}") from exc
    return plan.clip_path


# ---------------------------------------------------------------------------
# Blind verifier backends


@dataclass
class BlindDescription:
    setting: str = ""
    activities: list[str] = field(default_factory=list)
    people_count: int | None = None
    visible_text: list[str] = field(default_factory=list)
    language: str = ""
    audio_summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def bag(self) -> set[str]:
        tokens: set[str] = set()
        for part in (self.setting, self.language, self.audio_summary, *self.activities, *self.visible_text):
            tokens.update(_tokens(str(part)))
        return tokens

    def years(self) -> set[str]:
        joined = " ".join((*self.visible_text, self.audio_summary))
        return set(_YEAR_RE.findall(joined))

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("raw", None)
        return payload


class VerifierBackend(Protocol):
    """Blindly describes a clip. Implementations MUST NOT receive the claim.

    ``transcript`` is raw source evidence (anonymized speech from the clip's
    range) — never claim text; speaker labels are anonymized so diarization
    naming cannot leak an identity hypothesis into the blind description.
    """

    name: str

    def describe_clip(
        self, clip_path: Path, *, include_audio: bool = True, transcript: str | None = None
    ) -> BlindDescription: ...


class FakeVerifierBackend:
    """Test backend: canned descriptions keyed by clip filename (or a default)."""

    name = "fake"

    def __init__(
        self,
        descriptions: dict[str, BlindDescription] | None = None,
        default: BlindDescription | None = None,
    ) -> None:
        self.descriptions = descriptions or {}
        self.default = default or BlindDescription()
        self.calls: list[dict[str, Any]] = []

    def describe_clip(
        self, clip_path: Path, *, include_audio: bool = True, transcript: str | None = None
    ) -> BlindDescription:
        self.calls.append(
            {"clip_path": Path(clip_path), "include_audio": include_audio, "transcript": transcript}
        )
        return self.descriptions.get(Path(clip_path).name, self.default)


_BLIND_PROMPT = (
    "You are describing home-video footage for an archival index. You are given "
    "still frames sampled from a short clip (each labeled with its clip-relative "
    "second) and, when available, an anonymized transcript of the speech. "
    "Describe ONLY what is present. Return compact JSON with exactly these keys: "
    '{"setting": str (location/scene type), "activities": [str], '
    '"people_count": int|null (max distinct people visible), '
    '"visible_text": [str] (any signs/overlays/dates readable in frames), '
    '"language": str (language(s) spoken), "audio_summary": str (what is said/heard, '
    "including any names, dates, or places mentioned)}. Do not speculate beyond the "
    "frames and transcript."
)


def _extract_clip_frames(clip_path: Path) -> list[tuple[float, bytes]]:
    """Sample (clip_relative_s, jpeg_bytes) frames from an extracted clip."""

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(clip_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        duration = float(json.loads(probe.stdout or "{}").get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        duration = 0.0
    duration = max(duration, FRAME_MIN_INTERVAL_S)
    interval = max(FRAME_MIN_INTERVAL_S, duration / FRAME_MAX_COUNT)

    import tempfile

    frames: list[tuple[float, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="tapesplit-verify-") as tmp:
        pattern = str(Path(tmp) / "frame_%04d.jpg")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(clip_path),
                "-vf",
                f"fps=1/{interval:.3f},scale=-2:{FRAME_HEIGHT}",
                "-q:v",
                str(FRAME_JPEG_QUALITY),
                pattern,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        for i, path in enumerate(sorted(Path(tmp).glob("frame_*.jpg"))):
            frames.append((round(i * interval, 1), path.read_bytes()))
    return frames


def _frame_content_parts(frames: list[tuple[float, bytes]]) -> list[dict[str, Any]]:
    import base64

    parts: list[dict[str, Any]] = []
    for t, jpeg in frames:
        parts.append({"type": "text", "text": f"frame at {t:g}s:"})
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                    "detail": "low",
                },
            }
        )
    return parts


def _parse_blind_payload(payload: dict[str, Any]) -> BlindDescription:
    activities = payload.get("activities") or []
    visible = payload.get("visible_text") or []
    count = payload.get("people_count")
    return BlindDescription(
        setting=str(payload.get("setting") or ""),
        activities=[str(a) for a in activities if a] if isinstance(activities, list) else [],
        people_count=int(count) if isinstance(count, (int, float)) else None,
        visible_text=[str(v) for v in visible if v] if isinstance(visible, list) else [],
        language=str(payload.get("language") or ""),
        audio_summary=str(payload.get("audio_summary") or ""),
        raw=payload,
    )


class AzureVerifierBackend:
    """gpt-5.6-sol blind clip description via timestamped frame grids.

    sol has no video/audio input (live-verified), so a clip becomes N sampled
    frames — each preceded by a 'frame at Xs:' label — plus the anonymized
    transcript. Azure's content filter is known to refuse innocuous family
    footage; on ``content_policy_violation`` the frame batch is bisected and
    flagged frames dropped, degrading to a transcript-only description
    (annotated ``visual_blocked``) if nothing visual survives.

    Config: AZURE_OPENAI_* env plus TAPESPLIT_VERIFIER_BACKEND=azure opt-in;
    TAPESPLIT_VERIFIER_DEPLOYMENT overrides the deployment.
    """

    name = "azure"

    def __init__(
        self,
        deployment: str | None = None,
        project_dir: Path | None = None,
        completion_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.deployment = deployment or _verifier_deployment()
        self.project_dir = project_dir
        self._completion_fn = completion_fn

    # -- request plumbing ---------------------------------------------------

    def _completion(self, **kwargs: Any) -> dict[str, Any]:
        if self._completion_fn is not None:
            return self._completion_fn(**kwargs)
        from tapesplit.azure_openai_adapter import reasoning_chat_completion

        return reasoning_chat_completion(**kwargs)

    def _describe_request(
        self, frames: list[tuple[float, bytes]], transcript: str | None, *, probe: bool = False
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": _BLIND_PROMPT}]
        content.extend(_frame_content_parts(frames))
        if transcript:
            content.append(
                {"type": "text", "text": f"Anonymized transcript of the clip:\n{transcript}"}
            )
        if not frames:
            content.append(
                {
                    "type": "text",
                    "text": "No frames are available for this clip; describe from the "
                    'transcript alone and set "setting" to what can be inferred.',
                }
            )
        return self._completion(
            deployment=self.deployment,
            messages=[{"role": "user", "content": content}],
            max_completion_tokens=16 if probe else 1500,
            reasoning_effort="low" if probe else _verifier_effort(),
            response_format={"type": "json_object"},
            project_dir=self.project_dir,
            operation="verify_describe_probe" if probe else "verify_describe",
        )

    def _admissible_frames(
        self, frames: list[tuple[float, bytes]], transcript: str | None, budget: list[int]
    ) -> list[tuple[float, bytes]]:
        """Largest filter-passing subset of ``frames`` via bisection probes."""

        from tapesplit.azure_openai_adapter import ContentPolicyViolation

        if not frames or budget[0] <= 0:
            return []
        budget[0] -= 1
        try:
            self._describe_request(frames, transcript, probe=True)
            return frames
        except ContentPolicyViolation:
            if len(frames) == 1:
                return []
            mid = len(frames) // 2
            return self._admissible_frames(frames[:mid], transcript, budget) + self._admissible_frames(
                frames[mid:], transcript, budget
            )

    # -- public -------------------------------------------------------------

    def describe_clip(
        self, clip_path: Path, *, include_audio: bool = True, transcript: str | None = None
    ) -> BlindDescription:
        from tapesplit.azure_openai_adapter import (
            ContentPolicyViolation,
            load_azure_openai_config,
        )

        config = load_azure_openai_config()
        if not config.configured or not _verifier_opted_in():
            raise VerifierNotConfigured(
                "Azure verifier needs AZURE_OPENAI_* config and TAPESPLIT_VERIFIER_BACKEND=azure"
            )

        frames = _extract_clip_frames(clip_path)
        dropped = 0
        visual_blocked = False
        try:
            result = self._describe_request(frames, transcript if include_audio else None)
        except ContentPolicyViolation:
            budget = [CONTENT_FILTER_MAX_PROBES]
            surviving = self._admissible_frames(frames, transcript, budget)
            dropped = len(frames) - len(surviving)
            frames = surviving
            if not frames:
                visual_blocked = True
            result = self._describe_request(frames, transcript if include_audio else None)

        message = ((result.get("choices") or [{}])[0].get("message") or {})
        try:
            payload = json.loads(message.get("content") or "{}")
        except (ValueError, TypeError):
            payload = {}
        description = _parse_blind_payload(payload if isinstance(payload, dict) else {})
        description.raw = {
            **description.raw,
            "frames_used": len(frames),
            "frames_dropped_by_filter": dropped,
            "visual_blocked": visual_blocked,
            "deployment": self.deployment,
        }
        return description


def _verifier_opted_in() -> bool:
    return os.environ.get("TAPESPLIT_VERIFIER_BACKEND", "").strip().lower() == "azure"


def _verifier_deployment() -> str:
    return os.environ.get("TAPESPLIT_VERIFIER_DEPLOYMENT", "").strip() or DEFAULT_VERIFIER_DEPLOYMENT


def _verifier_effort() -> str:
    return os.environ.get("TAPESPLIT_VERIFIER_EFFORT", "").strip() or "medium"


def _adjudicator_deployment() -> str:
    return (
        os.environ.get("TAPESPLIT_ADJUDICATOR_DEPLOYMENT", "").strip()
        or DEFAULT_ADJUDICATOR_DEPLOYMENT
    )


def check_verification_config() -> dict[str, Any]:
    """Capability probe consumed by ``auto.gather_capabilities``."""

    backend = "unavailable"
    if _verifier_opted_in():
        from tapesplit.azure_openai_adapter import load_azure_openai_config

        if load_azure_openai_config().configured:
            backend = "azure"
    return {
        "verification_backend": backend,
        "verification_deployment": _verifier_deployment(),
    }


def resolve_verifier_backend(
    name: str | None = None, project_dir: Path | None = None
) -> VerifierBackend | None:
    """The configured verifier backend, or None when verification is off."""

    requested = (name or "").strip().lower()
    if requested == "azure" or (not requested and _verifier_opted_in()):
        from tapesplit.azure_openai_adapter import load_azure_openai_config

        if load_azure_openai_config().configured:
            return AzureVerifierBackend(project_dir=project_dir)
        if requested:
            raise VerifierNotConfigured("Azure OpenAI endpoint/key/api-version not configured")
    return None


def resolve_adjudicator(name: str | None = None, project_dir: Path | None = None) -> Adjudicator:
    """Adjudicator by name: 'llm' → terra semantic judge, else keyword baseline."""

    requested = (name or "").strip().lower()
    if requested in ("llm", "terra"):
        return LlmAdjudicator(project_dir=project_dir)
    return KeywordOverlapAdjudicator()


# ---------------------------------------------------------------------------
# Clip transcripts (blind: raw speech only, anonymized speakers)


def _anonymize_speaker(label: str, mapping: dict[str, str]) -> str:
    if label not in mapping:
        mapping[label] = f"Speaker {len(mapping) + 1}"
    return mapping[label]


class _TranscriptIndex:
    """Per-video speech lines for clip ranges; speaker labels anonymized.

    Prefers the diarized segments (they carry cleaner cloud transcripts in
    metadata.transcript_text); falls back to whisper transcript_segments.
    Anonymization is deliberate: diarization names ('Filia') are an identity
    hypothesis, and leaking them into the blind description would let a
    person_presence claim confirm itself.
    """

    def __init__(self, project: Path) -> None:
        self._by_video: dict[str, list[tuple[float, float, str, str]]] = {}
        speaker_rows = read_jsonl(project / "speaker_segments.jsonl")
        for row in speaker_rows:
            text = str((row.get("metadata") or {}).get("transcript_text") or "").strip()
            if not text:
                continue
            video = str(row.get("source_video_id") or "")
            self._by_video.setdefault(video, []).append(
                (
                    float(row.get("start_s") or 0.0),
                    float(row.get("end_s") or 0.0),
                    str(row.get("speaker_label") or ""),
                    text,
                )
            )
        if not self._by_video:
            for row in read_jsonl(project / "transcript_segments.jsonl"):
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                video = str(row.get("source_video_id") or "")
                self._by_video.setdefault(video, []).append(
                    (float(row.get("start_s") or 0.0), float(row.get("end_s") or 0.0), "", text)
                )
        for rows in self._by_video.values():
            rows.sort(key=lambda r: r[0])

    def for_range(self, video: str, start_s: float, end_s: float, *, max_chars: int = 4000) -> str | None:
        rows = self._by_video.get(video) or []
        mapping: dict[str, str] = {}
        lines: list[str] = []
        for seg_start, seg_end, label, text in rows:
            if seg_end < start_s or seg_start > end_s:
                continue
            stamp = max(0.0, seg_start - start_s)
            who = _anonymize_speaker(label, mapping) if label else "Speaker"
            lines.append(f"[{stamp:.0f}s] {who}: {text}")
        if not lines:
            return None
        joined = "\n".join(lines)
        return joined[:max_chars]


# ---------------------------------------------------------------------------
# Adjudication


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reason: str
    score: float | None = None


class Adjudicator(Protocol):
    name: str

    def adjudicate(self, claim: Claim, description: BlindDescription) -> Verdict: ...


class KeywordOverlapAdjudicator:
    """Deterministic offline baseline: token overlap between claim and blind
    description. An LLM adjudicator can replace this via the same interface.
    """

    name = "keyword-overlap"

    def adjudicate(self, claim: Claim, description: BlindDescription) -> Verdict:
        if claim.claim_type == "event_date":
            year = claim.keywords[0] if claim.keywords else ""
            seen_years = description.years()
            if year and year in seen_years:
                return Verdict(VERDICT_SUPPORTED, f"year {year} visible/audible in clip", 1.0)
            if seen_years:
                return Verdict(
                    VERDICT_CONTRADICTED,
                    f"clip shows year(s) {sorted(seen_years)}, claim says {year}",
                    0.0,
                )
            return Verdict(VERDICT_UNDECIDABLE, "no year evidence in a short clip", None)

        bag = description.bag()
        if claim.claim_type == "person_presence":
            named = [k for k in claim.keywords if k in bag]
            if named:
                return Verdict(VERDICT_SUPPORTED, f"name evidence in clip: {named}", 1.0)
            # Absence of a name is not evidence of absence for a keyword
            # baseline — identity needs a face-capable adjudicator.
            return Verdict(VERDICT_UNDECIDABLE, "identity needs face-capable verification", None)

        keywords = [k for k in claim.keywords if len(k) > 2]
        if not keywords:
            return Verdict(VERDICT_UNDECIDABLE, "claim has no content keywords", None)
        overlap = [k for k in keywords if k in bag]
        score = len(overlap) / len(keywords)
        if score >= SUPPORT_MIN_OVERLAP:
            return Verdict(VERDICT_SUPPORTED, f"description matches claim terms {overlap[:6]}", round(score, 3))
        if not overlap and len(bag) >= CONTRADICT_MIN_DESCRIPTION_TOKENS:
            return Verdict(
                VERDICT_CONTRADICTED,
                "rich description shares nothing with the claim",
                0.0,
            )
        return Verdict(VERDICT_UNDECIDABLE, f"weak overlap {round(score, 3)}", round(score, 3))


_ADJUDICATOR_PROMPT = (
    "You compare an archival claim about a home-video clip against a BLIND "
    "description of that clip produced by a model that never saw the claim. "
    "Judge only whether the description supports or contradicts the claim; "
    "cross-language matches count (e.g. an English claim supported by Russian "
    "speech about the same content). Absence of evidence in a short clip is "
    "UNDECIDABLE, not contradiction — unless the description richly shows "
    "something incompatible with the claim. Return compact JSON: "
    '{"verdict": "SUPPORTED"|"CONTRADICTED"|"UNDECIDABLE", '
    '"reason": str (<=40 words, cite the decisive evidence), '
    '"confidence": number 0..1}'
)


class LlmAdjudicator:
    """gpt-5.6-terra semantic adjudication; keyword baseline as fallback.

    Sees the claim AND the blind description — never the footage. Falls back
    to :class:`KeywordOverlapAdjudicator` on transport errors so a flaky call
    degrades to the offline verdict instead of losing the sample.
    """

    def __init__(
        self,
        deployment: str | None = None,
        project_dir: Path | None = None,
        completion_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.deployment = deployment or _adjudicator_deployment()
        self.name = f"llm-{self.deployment}"
        self.project_dir = project_dir
        self._completion_fn = completion_fn
        self._fallback = KeywordOverlapAdjudicator()

    def _completion(self, **kwargs: Any) -> dict[str, Any]:
        if self._completion_fn is not None:
            return self._completion_fn(**kwargs)
        from tapesplit.azure_openai_adapter import reasoning_chat_completion

        return reasoning_chat_completion(**kwargs)

    def adjudicate(self, claim: Claim, description: BlindDescription) -> Verdict:
        packet = {
            "claim_type": claim.claim_type,
            "claim": claim.text,
            "claim_keywords": list(claim.keywords)[:12],
            "clip_seconds": round(claim.end_s - claim.start_s, 1),
            "blind_description": description.to_payload(),
            "visual_blocked": bool(description.raw.get("visual_blocked")),
        }
        try:
            result = self._completion(
                deployment=self.deployment,
                messages=[
                    {
                        "role": "user",
                        "content": _ADJUDICATOR_PROMPT + "\n\n" + json.dumps(packet, ensure_ascii=False),
                    }
                ],
                max_completion_tokens=350,
                reasoning_effort="low",
                response_format={"type": "json_object"},
                project_dir=self.project_dir,
                operation="verify_adjudicate",
            )
            message = ((result.get("choices") or [{}])[0].get("message") or {})
            payload = json.loads(message.get("content") or "{}")
            verdict = str(payload.get("verdict") or "").upper()
            if verdict not in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_UNDECIDABLE):
                raise ValueError(f"bad verdict: {verdict!r}")
            confidence = payload.get("confidence")
            score = float(confidence) if isinstance(confidence, (int, float)) else None
            return Verdict(verdict, str(payload.get("reason") or ""), score)
        except Exception as exc:  # noqa: BLE001 - degrade, don't lose the sample
            fallback = self._fallback.adjudicate(claim, description)
            return Verdict(
                fallback.verdict,
                f"llm-fallback ({type(exc).__name__}): {fallback.reason}",
                fallback.score,
            )


# ---------------------------------------------------------------------------
# Run + report


def _next_verification_index(existing: list[dict[str, Any]]) -> int:
    highest = 0
    for row in existing:
        match = re.match(r"verification_(\d+)$", str(row.get("id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def run_verification(
    project_dir: Path,
    *,
    backend: VerifierBackend | None,
    adjudicator: Adjudicator | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
    types: tuple[str, ...] | None = None,
    dry_run: bool = False,
    force_clips: bool = False,
    clip_extractor: Callable[[ClipPlan], Path] | None = None,
) -> dict[str, Any]:
    """Sample claims, extract clips, blind-describe, adjudicate, record."""

    project = project_dir.expanduser().resolve()
    claims = enumerate_claims(project)
    sampled = sample_claims(claims, sample_size=sample_size, seed=seed, types=types)
    plans = plan_clips(project, sampled)

    by_type: dict[str, int] = {}
    for claim in sampled:
        by_type[claim.claim_type] = by_type.get(claim.claim_type, 0) + 1

    if dry_run:
        return {
            "dry_run": True,
            "claims_enumerated": len(claims),
            "sampled": len(sampled),
            "by_type": by_type,
            "plan": [
                {
                    "claim_id": plan.claim.id,
                    "claim_type": plan.claim.claim_type,
                    "target_id": plan.claim.target_id,
                    "source_video_id": plan.claim.source_video_id,
                    "clip_start_s": plan.clip_start_s,
                    "clip_end_s": plan.clip_end_s,
                    "text": plan.claim.text,
                    "weight_reasons": list(plan.claim.weight_reasons),
                    "source_found": plan.source_path is not None,
                }
                for plan in plans
            ],
        }

    if backend is None:
        raise VerifierNotConfigured(
            "no verifier backend (set TAPESPLIT_VERIFIER_BACKEND=azure or pass one explicitly)"
        )
    judge = adjudicator or KeywordOverlapAdjudicator()
    extract = clip_extractor or (lambda plan: extract_clip(plan, force=force_clips))
    transcripts = _TranscriptIndex(project)

    existing = read_jsonl(project / VERIFICATIONS_FILENAME)
    index = _next_verification_index(existing)
    verdict_counts = {VERDICT_SUPPORTED: 0, VERDICT_CONTRADICTED: 0, VERDICT_UNDECIDABLE: 0}
    errors: list[dict[str, str]] = []
    written = 0

    for plan in plans:
        claim = plan.claim
        try:
            clip_path = extract(plan)
            # Blind protocol: the backend receives the clip and raw anonymized
            # speech from its range — never the claim.
            transcript = transcripts.for_range(
                claim.source_video_id, plan.clip_start_s, plan.clip_end_s
            )
            description = backend.describe_clip(
                clip_path, include_audio=True, transcript=transcript
            )
        except (RuntimeError, NotImplementedError) as exc:
            errors.append({"claim_id": claim.id, "error": str(exc)})
            continue
        verdict = judge.adjudicate(claim, description)
        verdict_counts[verdict.verdict] = verdict_counts.get(verdict.verdict, 0) + 1
        row = {
            "id": f"verification_{index:06d}",
            "claim_id": claim.id,
            "claim_type": claim.claim_type,
            "action": claim.action,
            "target_id": claim.target_id,
            "target_type": claim.target_type,
            "source_video_id": claim.source_video_id,
            "start_s": claim.start_s,
            "end_s": claim.end_s,
            "clip_path": str(plan.clip_path.relative_to(project)),
            "claim_text": claim.text,
            "claim_confidence": claim.confidence,
            "weight_reasons": list(claim.weight_reasons),
            "blind_description": description.to_payload(),
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "score": verdict.score,
            "verifier": backend.name,
            "adjudicator": judge.name,
            "reviewer": MACHINE_REVIEWER,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        append_jsonl(project / VERIFICATIONS_FILENAME, row)
        index += 1
        written += 1

    _write_contradiction_flags(project)

    supported = verdict_counts[VERDICT_SUPPORTED]
    contradicted = verdict_counts[VERDICT_CONTRADICTED]
    decidable = supported + contradicted
    return {
        "dry_run": False,
        "claims_enumerated": len(claims),
        "sampled": len(sampled),
        "verified": written,
        "by_type": by_type,
        "verdicts": verdict_counts,
        "grounded_precision": round(supported / decidable, 4) if decidable else None,
        "errors": errors or None,
        "output": str(project / VERIFICATIONS_FILENAME),
    }


def _write_contradiction_flags(project: Path) -> None:
    """Refresh the CONTRADICTED digest the review surface can pick up.

    TODO(seam): fold these into visualization.py's review queue as
    first-class review items (resolve_verification action) instead of a
    sidecar file; kept separate for now to avoid entangling the queue
    builder.
    """

    rows = read_jsonl(project / VERIFICATIONS_FILENAME)
    flags = [
        {
            "id": f"verification_flag_{i:06d}",
            "verification_id": row.get("id"),
            "claim_type": row.get("claim_type"),
            "action": row.get("action"),
            "target_id": row.get("target_id"),
            "target_type": row.get("target_type"),
            "source_video_id": row.get("source_video_id"),
            "claim_text": row.get("claim_text"),
            "reason": row.get("reason"),
            "clip_path": row.get("clip_path"),
        }
        for i, row in enumerate(
            (r for r in rows if r.get("verdict") == VERDICT_CONTRADICTED), start=1
        )
    ]
    path = project / VERIFICATION_FLAGS_FILENAME
    payload = "".join(json.dumps(flag, sort_keys=True) + "\n" for flag in flags)
    path.write_text(payload, encoding="utf-8")


def _chunked_tapes(project: Path) -> set[str]:
    """Tapes analyzed in multiple chunks (chunk_index ≥ 2 observed)."""

    chunked: set[str] = set()
    for row in read_jsonl(project / "gemini_events.jsonl"):
        metadata = row.get("metadata") or {}
        index = metadata.get("chunk_index")
        video = str(row.get("source_video_id") or metadata.get("source_video_id") or "")
        if video and isinstance(index, int) and index >= 2:
            chunked.add(video)
    return chunked


def verification_report(project_dir: Path) -> dict[str, Any]:
    """Grounded-precision metrics per claim type and per tape.

    The chunked-vs-whole comparison is first-class: it is the instrument that
    shows whether chunk-analyzed tapes' claims are less grounded than
    whole-tape claims (and whether a fix moved the needle).
    """

    project = project_dir.expanduser().resolve()
    rows = read_jsonl(project / VERIFICATIONS_FILENAME)
    chunked = _chunked_tapes(project)

    def bucket() -> dict[str, Any]:
        return {"sampled": 0, "supported": 0, "contradicted": 0, "undecidable": 0}

    def precision(counts: dict[str, Any]) -> float | None:
        decidable = counts["supported"] + counts["contradicted"]
        return round(counts["supported"] / decidable, 4) if decidable else None

    overall = bucket()
    by_type: dict[str, dict[str, Any]] = {}
    by_tape: dict[str, dict[str, Any]] = {}
    chunk_groups = {"chunked": bucket(), "whole": bucket()}

    for row in rows:
        verdict = str(row.get("verdict") or "")
        key = {
            VERDICT_SUPPORTED: "supported",
            VERDICT_CONTRADICTED: "contradicted",
            VERDICT_UNDECIDABLE: "undecidable",
        }.get(verdict)
        if key is None:
            continue
        video = str(row.get("source_video_id") or "")
        targets = [
            overall,
            by_type.setdefault(str(row.get("claim_type") or "unknown"), bucket()),
            by_tape.setdefault(video or "unknown", bucket()),
            chunk_groups["chunked" if video in chunked else "whole"],
        ]
        for target in targets:
            target["sampled"] += 1
            target[key] += 1

    for counts in (overall, *by_type.values(), *by_tape.values(), *chunk_groups.values()):
        counts["grounded_precision"] = precision(counts)

    return {
        "verifications": len(rows),
        "overall": overall,
        "by_claim_type": by_type,
        "by_tape": by_tape,
        "chunked_vs_whole": chunk_groups,
        "chunked_tapes": sorted(chunked),
    }

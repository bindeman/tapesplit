"""Closed-loop verification: blind clip checks that grade the pipeline's claims."""

import json
from pathlib import Path

import pytest

from tapesplit.auto import AutoOptions, plan_auto
from tapesplit.calibration import (
    MACHINE_OBSERVATION_WEIGHT,
    analyze_review_outcomes,
    observed_precision,
)
from tapesplit.cli import main
from tapesplit.storage import append_jsonl, write_json
from tapesplit.verification import (
    BlindDescription,
    FakeVerifierBackend,
    KeywordOverlapAdjudicator,
    VerifierNotConfigured,
    enumerate_claims,
    run_verification,
    sample_claims,
    verification_report,
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    write_json(project / "manifest.json", {"schema_version": 1})
    for index, filename in ((1, "tape-a.mp4"), (2, "tape-b.mp4")):
        append_jsonl(
            project / "tapes.jsonl",
            {
                "id": f"video_{index:06d}",
                "filename": filename,
                "path": str(tmp_path / filename),
                "relative_path": filename,
                "probe": {"duration_s": 3600.0},
            },
        )
    return project


def _event(
    index: int,
    video: str,
    start: float,
    end: float,
    *,
    title: str,
    summary: str = "",
    confidence: float = 0.9,
    source_event_ids: list[str] | None = None,
    date_candidates: list[str] | None = None,
) -> dict:
    return {
        "id": f"canonical_event_{index:06d}",
        "title": title,
        "summary": summary,
        "confidence": confidence,
        "start_s": start,
        "end_s": end,
        "metadata": {
            "source_event_ids": source_event_ids or [],
            "date_candidates": date_candidates or [],
            "source_ranges": [
                {"source_video_id": video, "start_s": start, "end_s": end}
            ],
        },
    }


def _seed_claim_artifacts(project: Path) -> None:
    events = [
        _event(1, "video_000001", 0.0, 60.0, title="Backyard birthday party", summary="Kids around a cake"),
        _event(2, "video_000001", 100.0, 160.0, title="Geology lab tour", summary="Rock samples on tables"),
        _event(
            3,
            "video_000002",
            0.0,
            90.0,
            title="Skiing at the resort",
            summary="Downhill runs in fresh snow",
            confidence=0.4,
            source_event_ids=["gem_event_000001"],
            date_candidates=["Christmas 1998"],
        ),
        _event(4, "video_000002", 200.0, 260.0, title="Dinner at home", summary="Family around the table"),
    ]
    for event in events:
        append_jsonl(project / "canonical_events.jsonl", event)

    # gem_event_000001 sits at a chunk seam (chunk 2 of a chunked tape).
    append_jsonl(
        project / "gemini_events.jsonl",
        {
            "id": "gem_event_000001",
            "source_video_id": "video_000002",
            "start_s": 900.0,
            "end_s": 950.0,
            "metadata": {"chunk_id": "chunk_0002", "chunk_index": 2, "chunk_start_s": 885.0, "chunk_end_s": 1785.0},
        },
    )
    append_jsonl(
        project / "event_place_roles.jsonl",
        {
            "id": "event_place_role_000001",
            "canonical_event_id": "canonical_event_000002",
            "confidence": 0.8,
            "candidate_options": [
                {"id": "selected", "selected": True, "label": "University Geology Lab", "role": "visible_place"}
            ],
        },
    )
    append_jsonl(
        project / "face_identity_candidates.jsonl",
        {
            "id": "face_identity_candidate_000001",
            "person_label": "Anna",
            "confidence": 0.7,
            "supporting_event_ids": ["canonical_event_000001"],
        },
    )
    # canonical_event_000004 was auto-accepted by the pipeline.
    append_jsonl(
        project / "corrections.jsonl",
        {"action": "confirm_event", "target_id": "canonical_event_000004", "reviewer": "auto-pipeline"},
    )


def _fake_extractor(plan):
    plan.clip_path.parent.mkdir(parents=True, exist_ok=True)
    plan.clip_path.write_bytes(b"clip")
    return plan.clip_path


def test_enumerate_claims_types_and_oversampling_tags(tmp_path: Path):
    project = _project(tmp_path)
    _seed_claim_artifacts(project)

    claims = enumerate_claims(project)
    by_type = {}
    for claim in claims:
        by_type.setdefault(claim.claim_type, []).append(claim)

    assert len(by_type["event_content"]) == 4
    assert len(by_type["event_place"]) == 1
    assert len(by_type["event_date"]) == 1
    assert len(by_type["person_presence"]) == 1

    skiing = next(c for c in by_type["event_content"] if "Skiing" in c.text)
    assert "chunk-boundary" in skiing.weight_reasons
    assert "low-confidence" in skiing.weight_reasons

    dinner = next(c for c in by_type["event_content"] if "Dinner" in c.text)
    assert "auto-accepted" in dinner.weight_reasons

    date_claim = by_type["event_date"][0]
    assert date_claim.keywords == ("1998",)


def test_sampler_is_deterministic_and_stratified(tmp_path: Path):
    project = _project(tmp_path)
    _seed_claim_artifacts(project)
    claims = enumerate_claims(project)

    first = [c.id for c in sample_claims(claims, sample_size=6, seed=7)]
    second = [c.id for c in sample_claims(claims, sample_size=6, seed=7)]
    assert first == second

    sampled = sample_claims(claims, sample_size=6, seed=7)
    strata = {(c.source_video_id, c.claim_type) for c in sampled}
    # Both tapes and multiple claim types represented before any stratum repeats.
    assert {"video_000001", "video_000002"} <= {video for video, _ in strata}
    assert len(strata) >= 4

    typed = sample_claims(claims, sample_size=10, seed=7, types=("event_place",))
    assert [c.claim_type for c in typed] == ["event_place"]


def test_run_verification_blind_protocol_and_verdicts(tmp_path: Path):
    project = _project(tmp_path)
    _seed_claim_artifacts(project)

    matching = BlindDescription(
        setting="a backyard party",
        activities=["children eating birthday cake"],
        people_count=6,
    )
    backend = FakeVerifierBackend(default=matching)

    result = run_verification(
        project,
        backend=backend,
        sample_size=4,
        seed=1,
        types=("event_content",),
        clip_extractor=_fake_extractor,
    )

    assert result["verified"] == 4
    # Blind protocol: the backend saw only clip paths + audio flag, never claims.
    assert backend.calls
    for call in backend.calls:
        assert set(call.keys()) == {"clip_path", "include_audio"}
        assert call["clip_path"].suffix == ".mp4"

    rows = [json.loads(line) for line in (project / "verifications.jsonl").read_text().splitlines()]
    assert len(rows) == 4
    assert all(row["reviewer"] == "clip-verifier" for row in rows)
    birthday = next(r for r in rows if "birthday" in r["claim_text"].lower())
    assert birthday["verdict"] == "SUPPORTED"

    # A rich, disjoint description contradicts; contradictions are flagged.
    contradicted = [r for r in rows if r["verdict"] == "CONTRADICTED"]
    flags = [json.loads(line) for line in (project / "verification_flags.jsonl").read_text().splitlines()]
    assert len(flags) == len(contradicted)


def test_adjudicator_verdicts():
    judge = KeywordOverlapAdjudicator()
    claims = enumerate_claims.__module__  # silence linters about unused import patterns

    from tapesplit.verification import Claim

    content = Claim(
        id="claim_000001",
        claim_type="event_content",
        action="confirm_event",
        source_video_id="video_000001",
        start_s=0.0,
        end_s=10.0,
        text="Geology lab tour",
        keywords=("geology", "lab", "tour", "rock", "samples"),
        target_id="canonical_event_000001",
        target_type="canonical_event",
    )
    supporting = BlindDescription(setting="a geology lab", activities=["looking at rock samples"])
    assert judge.adjudicate(content, supporting).verdict == "SUPPORTED"

    contradicting = BlindDescription(
        setting="snowy mountain slope",
        activities=["people skiing downhill", "chairlift rides in the background"],
    )
    assert judge.adjudicate(content, contradicting).verdict == "CONTRADICTED"

    sparse = BlindDescription(setting="unclear footage")
    assert judge.adjudicate(content, sparse).verdict == "UNDECIDABLE"

    date_claim = Claim(
        id="claim_000002",
        claim_type="event_date",
        action="confirm_event_date",
        source_video_id="video_000001",
        start_s=0.0,
        end_s=10.0,
        text="This footage was captured in 1998.",
        keywords=("1998",),
        target_id="canonical_event_000001",
        target_type="canonical_event",
    )
    dated = BlindDescription(visible_text=["Christmas 1998"])
    assert judge.adjudicate(date_claim, dated).verdict == "SUPPORTED"
    misdated = BlindDescription(visible_text=["July 2004"])
    assert judge.adjudicate(date_claim, misdated).verdict == "CONTRADICTED"
    assert judge.adjudicate(date_claim, BlindDescription()).verdict == "UNDECIDABLE"

    person = Claim(
        id="claim_000003",
        claim_type="person_presence",
        action="confirm_identity",
        source_video_id="video_000001",
        start_s=0.0,
        end_s=10.0,
        text="Anna appears in this footage.",
        keywords=("anna",),
        target_id="face_identity_candidate_000001",
        target_type="face_identity_candidate",
    )
    assert judge.adjudicate(person, BlindDescription(setting="a party")).verdict == "UNDECIDABLE"


def test_run_verification_requires_backend_unless_dry_run(tmp_path: Path):
    project = _project(tmp_path)
    _seed_claim_artifacts(project)

    plan = run_verification(project, backend=None, sample_size=3, seed=0, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["sampled"] == 3
    assert all("clip_start_s" in item for item in plan["plan"])

    with pytest.raises(VerifierNotConfigured):
        run_verification(project, backend=None, sample_size=3, seed=0)


def test_verification_report_chunked_vs_whole(tmp_path: Path):
    project = _project(tmp_path)
    _seed_claim_artifacts(project)

    supported = BlindDescription(
        setting="geology lab with rock samples",
        activities=["skiing downhill in fresh snow", "birthday cake with kids", "family dinner at the table"],
    )
    backend = FakeVerifierBackend(default=supported)
    run_verification(
        project,
        backend=backend,
        sample_size=6,
        seed=3,
        clip_extractor=_fake_extractor,
    )

    report = verification_report(project)
    assert report["verifications"] == 6
    assert report["chunked_tapes"] == ["video_000002"]
    chunked = report["chunked_vs_whole"]["chunked"]
    whole = report["chunked_vs_whole"]["whole"]
    assert chunked["sampled"] + whole["sampled"] == 6
    assert set(report["by_tape"]) <= {"video_000001", "video_000002"}


def test_machine_verdicts_blend_into_calibration_at_reduced_weight(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    rows = [
        {"action": "confirm_event", "target_id": "ce_1", "reviewer": "phillip"},
        {"action": "rename_event", "target_id": "ce_2", "reviewer": "phillip"},
    ]
    (project / "corrections.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    for verdict in ("SUPPORTED", "SUPPORTED", "CONTRADICTED"):
        append_jsonl(
            project / "verifications.jsonl",
            {"action": "confirm_event", "verdict": verdict, "reviewer": "clip-verifier"},
        )

    outcomes = analyze_review_outcomes(project)
    counts = outcomes["confirm_event"]
    assert counts["machine_supported"] == 2
    assert counts["machine_contradicted"] == 1

    blended, blended_obs = observed_precision(counts)
    human, human_obs = observed_precision(counts, include_machine=False)
    assert human_obs == 2
    assert blended_obs == 2 + MACHINE_OBSERVATION_WEIGHT * 3
    # Two machine supports vs one contradiction pull precision up.
    assert blended > human


def test_auto_plan_verify_stage_gates_on_backend(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    project.mkdir()
    write_json(project / "manifest.json", {"schema_version": 1})
    append_jsonl(project / "tapes.jsonl", {"id": "video_000001", "probe": {"duration_s": 10.0}})

    caps = {"ffmpeg": True, "ffprobe": True, "verification_backend": "unavailable"}
    plan = plan_auto(project, AutoOptions(), caps, {"stages": {}})
    actions = {item.stage.name: item for item in plan}
    assert actions["verify"].action == "unavailable"
    assert "verifier backend" in actions["verify"].reason

    caps["verification_backend"] = "azure"
    plan = plan_auto(project, AutoOptions(), caps, {"stages": {}})
    actions = {item.stage.name: item for item in plan}
    assert actions["verify"].action == "run"


def test_cli_verify_dry_run_and_report(tmp_path: Path, capsys):
    project = _project(tmp_path)
    _seed_claim_artifacts(project)

    assert main(["verify", "run", str(project), "--dry-run", "--sample-size", "3"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert len(plan["plan"]) == 3

    assert main(["verify", "report", str(project)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verifications"] == 0

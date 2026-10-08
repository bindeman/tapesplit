from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl, write_json
from tapesplit.visibility import build_visibility_filter


JUDGMENT_SCORES = {
    "correct": 1.0,
    "mostly_correct": 0.75,
    "partial": 0.5,
    "incorrect": 0.0,
}

OPEN_JUDGMENTS = {"not_sure", "needs_followup", "skip"}


def build_eval_packet(
    project_dir: Path,
    *,
    out_dir: Path | None = None,
    max_items: int = 200,
    force: bool = False,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    output = (out_dir or (project / "eval_packet")).expanduser().resolve()
    if output == project:
        raise ValueError("--out cannot be the project directory")
    if output.exists():
        if not force:
            raise FileExistsError(f"{output} already exists; pass --force to replace it")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    items = _collect_eval_items(project, max_items=max_items)
    annotations = [_annotation_template(item) for item in items]

    _write_jsonl(output / "eval_items.jsonl", items)
    _write_jsonl(output / "annotations.template.jsonl", annotations)
    _write_csv(output / "eval_items.csv", items)
    _write_annotation_csv(output / "annotations.template.csv", annotations)
    _write_eval_sqlite(output / "eval.sqlite", items, annotations)
    _write_reviewer_readme(output / "README.md", project, items)
    write_json(
        output / "eval_manifest.json",
        {
            "project": str(project),
            "created_at": _now_iso(),
            "items": len(items),
            "by_task_type": dict(Counter(item["task_type"] for item in items)),
            "allowed_judgments": sorted([*JUDGMENT_SCORES, *OPEN_JUDGMENTS]),
            "files": {
                "items": "eval_items.jsonl",
                "items_csv": "eval_items.csv",
                "annotations_template": "annotations.template.jsonl",
                "annotations_template_csv": "annotations.template.csv",
                "sqlite": "eval.sqlite",
                "readme": "README.md",
            },
        },
    )
    return {
        "project": str(project),
        "output": str(output),
        "items": len(items),
        "by_task_type": dict(Counter(item["task_type"] for item in items)),
    }


def score_eval_packet(
    project_dir: Path,
    *,
    eval_dir: Path | None = None,
    annotations_path: Path | None = None,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    packet = _resolve_eval_dir(project, eval_dir)
    items = read_jsonl(packet / "eval_items.jsonl")
    annotations_file = _resolve_annotations_file(packet, annotations_path)
    annotations = _read_annotations(annotations_file)
    if not items:
        raise FileNotFoundError(f"no eval_items.jsonl found in {packet}")
    if not annotations:
        raise FileNotFoundError(f"no annotations found at {annotations_file}")

    items_by_id = {str(item.get("id")): item for item in items}
    scored_items = []
    followups = []
    by_task: dict[str, dict[str, Any]] = defaultdict(_metric_bucket)
    overall = _metric_bucket()

    for annotation in annotations:
        item_id = str(annotation.get("eval_item_id") or annotation.get("id") or "")
        item = items_by_id.get(item_id)
        if not item:
            continue
        judgment = _normalize_judgment(annotation.get("judgment"))
        score = JUDGMENT_SCORES.get(judgment)
        task_type = str(item.get("task_type") or "unknown")
        _add_metric(overall, judgment, score)
        _add_metric(by_task[task_type], judgment, score)
        scored = {
            "eval_item_id": item_id,
            "task_type": task_type,
            "source_record_type": item.get("source_record_type"),
            "source_id": item.get("source_id"),
            "predicted_label": item.get("predicted_label"),
            "judgment": judgment,
            "score": score,
            "needs_family_followup": _truthy(annotation.get("needs_family_followup")) or judgment == "needs_followup",
            "family_followup_question": str(annotation.get("family_followup_question") or ""),
            "corrected_value": annotation.get("corrected_value") or "",
            "notes": annotation.get("notes") or "",
            "reviewer": annotation.get("reviewer") or "",
            "reviewed_at": annotation.get("reviewed_at") or "",
        }
        scored_items.append(scored)
        if scored["needs_family_followup"] or judgment in {"not_sure", "needs_followup"}:
            followups.append({**scored, "prompt": item.get("prompt"), "evidence_summary": item.get("evidence_summary")})

    report = {
        "project": str(project),
        "eval_dir": str(packet),
        "annotations": str(annotations_file),
        "scored_at": _now_iso(),
        "items_total": len(items),
        "annotations_total": len(annotations),
        "matched_annotations": len(scored_items),
        "overall": _finalize_metric(overall),
        "by_task_type": {key: _finalize_metric(value) for key, value in sorted(by_task.items())},
        "followup_items": len(followups),
        "outputs": {
            "report": str(packet / "eval_report.json"),
            "scored_items": str(packet / "scored_items.jsonl"),
            "followups": str(packet / "family_followups.jsonl"),
        },
    }
    write_json(packet / "eval_report.json", report)
    _write_jsonl(packet / "scored_items.jsonl", scored_items)
    _write_jsonl(packet / "family_followups.jsonl", followups)
    _write_score_sqlite(packet / "eval.sqlite", scored_items, report)
    return report


def _collect_eval_items(project: Path, *, max_items: int) -> list[dict[str, Any]]:
    visibility = build_visibility_filter(project)
    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in evidence_rows if row.get("id")}
    all_event_ids = _all_event_ids(project)
    visible_event_ids = _visible_event_ids(project, visibility, evidence_by_id)
    rows: list[dict[str, Any]] = []
    rows.extend(_event_items(project, visibility, evidence_by_id))
    rows.extend(_album_items(project, visibility, evidence_by_id))
    rows.extend(_place_role_items(project, visibility, evidence_by_id))
    rows.extend(_place_items(project, visibility, evidence_by_id))
    rows.extend(_people_items(project, visibility, evidence_by_id))
    rows.extend(_date_items(project, visibility, evidence_by_id))
    rows.extend(_relationship_items(project, visibility, evidence_by_id))
    rows.extend(_continuity_items(project, visibility, evidence_by_id))
    rows.extend(_context_edge_items(project, visibility, evidence_by_id))
    rows = [
        row
        for row in rows
        if _eval_item_has_visible_event_context(
            row,
            all_event_ids=all_event_ids,
            visible_event_ids=visible_event_ids,
        )
    ]
    rows = sorted(rows, key=_eval_item_sort_key)
    if max_items > 0:
        rows = rows[:max_items]
    for index, row in enumerate(rows, start=1):
        row["id"] = f"eval_item_{index:06d}"
    return rows


def _event_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "canonical_events.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        items.append(
            _eval_item(
                task_type="event_correctness",
                source_record_type="event",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this event real family/home-video content, and are the title, summary, people, places, and date mostly correct?",
                predicted_label=row.get("title"),
                predicted_summary=row.get("summary"),
                predicted_fields={
                    "title": row.get("title"),
                    "summary": row.get("summary"),
                    "event_type": metadata.get("event_type"),
                    "people": metadata.get("people") or [],
                    "places": metadata.get("place_candidates") or [],
                    "dates": metadata.get("date_candidates") or [],
                    "relatedness": row.get("relatedness"),
                },
                reviewer_guidance=[
                    "Use correct if the event exists and the important metadata is right.",
                    "Use partial if the event is real but some people/place/date details are wrong.",
                    "Use needs_followup if you recognize the scene but need another family member to confirm.",
                ],
                tags=["event", str(metadata.get("event_type") or "unknown")],
            )
        )
    return items


def _album_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "albums.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="album_grouping",
                source_record_type="album",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Would this album/grouping be useful as a real family album, or should it be split/merged/renamed?",
                predicted_label=row.get("title"),
                predicted_summary=", ".join(str(item) for item in row.get("canonical_event_ids") or []),
                predicted_fields={
                    "title": row.get("title"),
                    "album_type": row.get("album_type"),
                    "date_label": row.get("date_label"),
                    "place_label": row.get("place_label"),
                    "people_labels": row.get("people_labels") or [],
                    "canonical_event_ids": row.get("canonical_event_ids") or [],
                },
                reviewer_guidance=[
                    "Use correct if the album would make sense to export.",
                    "Use partial if the title is useful but grouping needs edits.",
                    "Use incorrect if unrelated or unrelated events are mixed in.",
                ],
                tags=["album", str(row.get("album_type") or "unknown")],
            )
        )
    return items


def _place_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "place_groups.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="place_resolution",
                source_record_type="place_group",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this place label/scope right, and are the parent or nearby place links reasonable?",
                predicted_label=row.get("label"),
                predicted_summary="; ".join(str(note) for note in row.get("notes") or []),
                predicted_fields={
                    "label": row.get("label"),
                    "kind": row.get("kind"),
                    "place_type": row.get("place_type"),
                    "scope_label": row.get("scope_label"),
                    "aliases": row.get("aliases") or [],
                    "parent_place_labels": row.get("parent_place_labels") or [],
                    "nearby_place_labels": row.get("nearby_place_labels") or [],
                    "canonical_event_ids": row.get("canonical_event_ids") or [],
                },
                reviewer_guidance=[
                    "Use correct if the label and scope are useful, even if GPS is not known.",
                    "Use partial if the general place type is right but the city/name/scope is uncertain.",
                    "Mark needs_followup when someone else may know the exact place name.",
                ],
                tags=["place", str(row.get("place_type") or "unknown")],
                reachout_suggested=row.get("review_status") == "needs_review",
            )
        )
    return items


def _place_role_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "event_place_roles.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        role = str(row.get("role") or "")
        if role not in {"ambiguous_place_reference", "mentioned_destination", "travel_plan"}:
            continue
        items.append(
            _eval_item(
                task_type="place_role_claim",
                source_record_type="event_place_role",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this place role right, especially whether it should or should not count as the filming location?",
                predicted_label=f"{row.get('label')} -> {role}",
                predicted_summary="; ".join(str(text) for text in row.get("evidence_texts") or row.get("notes") or []),
                predicted_fields={
                    "label": row.get("label"),
                    "role": role,
                    "role_family": row.get("role_family"),
                    "include_in_place_groups": row.get("include_in_place_groups"),
                    "source_label": row.get("source_label"),
                    "canonical_event_id": row.get("canonical_event_id"),
                    "basis": row.get("basis") or [],
                },
                reviewer_guidance=[
                    "Use correct if the place was only mentioned or uncertain and should not become an album/GPS location.",
                    "Use incorrect if the place really is where the footage was filmed.",
                    "Use needs_followup when family context is needed to tell mention from location.",
                ],
                tags=["place_role", role],
                reachout_suggested=role == "ambiguous_place_reference",
            )
        )
    return items


def _people_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "people_groups.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="person_identity",
                source_record_type="people_group",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Do these aliases refer to one person/role, and is the display label useful?",
                predicted_label=row.get("label"),
                predicted_summary="; ".join(str(note) for note in row.get("notes") or []),
                predicted_fields={
                    "label": row.get("label"),
                    "kind": row.get("kind"),
                    "aliases": row.get("aliases") or [],
                    "canonical_event_ids": row.get("canonical_event_ids") or [],
                },
                reviewer_guidance=[
                    "Use correct only if aliases appear to refer to the same person.",
                    "Use partial if this is a useful role/group but not a confirmed identity.",
                    "Use needs_followup when a family member should identify the person.",
                ],
                tags=["person", str(row.get("kind") or "unknown")],
                reachout_suggested=row.get("review_status") == "needs_review",
            )
        )
    return items


def _date_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "date_groups.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="date_candidate",
                source_record_type="date_group",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this a recording/event date, a historical mention, or too uncertain to use?",
                predicted_label=row.get("label"),
                predicted_summary=str(row.get("date_value") or ""),
                predicted_fields={
                    "label": row.get("label"),
                    "date_value": row.get("date_value"),
                    "precision": row.get("precision"),
                    "source_kind": row.get("source_kind"),
                    "excluded_as_event_date": row.get("excluded_as_event_date"),
                    "canonical_event_ids": row.get("canonical_event_ids") or [],
                },
                reviewer_guidance=[
                    "Use correct if the date is correctly treated as event date or non-event mention.",
                    "Use partial if the date is close but precision is wrong.",
                    "Use needs_followup if another family member can confirm the date.",
                ],
                tags=["date", str(row.get("precision") or "unknown")],
                reachout_suggested=bool(row.get("excluded_as_event_date")) or row.get("review_status") == "needs_review",
            )
        )
    return items


def _relationship_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "relationship_candidates.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="relationship_candidate",
                source_record_type="relationship_candidate",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this relationship candidate supported by the tape, or should it remain unconfirmed?",
                predicted_label=f"{row.get('subject_label')} -> {row.get('predicate')} -> {row.get('object_label')}",
                predicted_summary="; ".join(str(signal) for signal in row.get("supporting_signals") or []),
                predicted_fields={
                    "subject_label": row.get("subject_label"),
                    "predicate": row.get("predicate"),
                    "object_label": row.get("object_label"),
                    "scope": row.get("scope") or {},
                    "supporting_signals": row.get("supporting_signals") or [],
                },
                reviewer_guidance=[
                    "Use correct only when the relationship is directly or strongly supported.",
                    "Use partial if the relation type is close but wording/person is wrong.",
                    "Use needs_followup for family-tree questions another person should verify.",
                ],
                tags=["relationship", str(row.get("predicate") or "unknown")],
                reachout_suggested=True,
            )
        )
    return items


def _context_edge_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    useful_predicates = {
        "inside_place_candidate",
        "nearby_time_place_context",
        "same_event_place_context",
        "within_region_candidate",
        "relationship_candidate_edge",
    }
    items = []
    for row in read_jsonl(project / "context_edges.jsonl"):
        if row.get("predicate") not in useful_predicates:
            continue
        if row.get("review_status") not in {"needs_review", "unreviewed"}:
            continue
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="context_edge",
                source_record_type="context_edge",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this context link useful for understanding the archive, or is it misleading?",
                predicted_label=f"{row.get('subject_label')} -> {row.get('predicate')} -> {row.get('object_label')}",
                predicted_summary="; ".join(str(signal) for signal in row.get("supporting_signals") or []),
                predicted_fields={
                    "subject_label": row.get("subject_label"),
                    "predicate": row.get("predicate"),
                    "object_label": row.get("object_label"),
                    "scope": row.get("scope") or {},
                    "supporting_signals": row.get("supporting_signals") or [],
                },
                reviewer_guidance=[
                    "Use correct if the edge is useful as context, even if it is not export-ready metadata.",
                    "Use incorrect if the link is misleading.",
                    "Use needs_followup if this is a family/location memory question.",
                ],
                tags=["context_edge", str(row.get("predicate") or "unknown")],
                reachout_suggested=row.get("review_status") == "needs_review",
            )
        )
    return items


def _continuity_items(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in read_jsonl(project / "event_continuity_contexts.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        items.append(
            _eval_item(
                task_type="continuity_context",
                source_record_type="event_continuity_context",
                row=row,
                evidence_by_id=evidence_by_id,
                prompt="Is this carried-forward location context useful, or does continuity break before this event?",
                predicted_label=f"{row.get('canonical_event_id')} appears to be in {row.get('context_label')}",
                predicted_summary="; ".join(str(signal) for signal in row.get("supporting_signals") or []),
                predicted_fields={
                    "canonical_event_id": row.get("canonical_event_id"),
                    "context_label": row.get("context_label"),
                    "anchor_event_ids": row.get("anchor_event_ids") or [],
                    "basis": row.get("basis") or [],
                    "not_exportable_as_gps": row.get("not_exportable_as_gps"),
                },
                reviewer_guidance=[
                    "Use correct if the nearby tape continuity makes this a useful context clue.",
                    "Use incorrect if a hard scene/location boundary occurs before the target event.",
                    "Use partial if the broad area is plausible but the exact carried context is too specific.",
                ],
                tags=["continuity", "place"],
                reachout_suggested=True,
            )
        )
    return items


def _eval_item(
    *,
    task_type: str,
    source_record_type: str,
    row: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    prompt: str,
    predicted_label: Any,
    predicted_summary: Any,
    predicted_fields: dict[str, Any],
    reviewer_guidance: list[str],
    tags: list[str],
    reachout_suggested: bool = False,
) -> dict[str, Any]:
    evidence_ids = [str(item) for item in row.get("evidence_ids") or [] if item]
    source_video_ids = _source_video_ids(row, evidence_ids, evidence_by_id)
    start_s = row.get("start_s") or row.get("first_start_s") or _scope_value(row, "start_s")
    end_s = row.get("end_s") or row.get("last_end_s") or _scope_value(row, "end_s")
    return {
        "id": "",
        "task_type": task_type,
        "source_record_type": source_record_type,
        "source_id": str(row.get("id") or ""),
        "source_video_id": source_video_ids[0] if source_video_ids else "",
        "source_video_ids": source_video_ids,
        "start_s": start_s,
        "end_s": end_s,
        "time_label": _range_label(start_s, end_s),
        "prompt": prompt,
        "predicted_label": str(predicted_label or ""),
        "predicted_summary": str(predicted_summary or ""),
        "predicted_fields": predicted_fields,
        "confidence": row.get("confidence"),
        "review_status": row.get("review_status") or "",
        "evidence_ids": evidence_ids,
        "evidence_summary": _evidence_summary(evidence_ids, evidence_by_id),
        "reviewer_guidance": reviewer_guidance,
        "reachout_suggested": reachout_suggested,
        "family_followup_question": _default_followup_question(task_type, predicted_label),
        "tags": [tag for tag in tags if tag],
    }


def _annotation_template(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "eval_item_id": item["id"],
        "judgment": "",
        "corrected_value": "",
        "needs_family_followup": bool(item.get("reachout_suggested")),
        "family_followup_question": item.get("family_followup_question", ""),
        "notes": "",
        "reviewer": "",
        "reviewed_at": "",
    }


def _write_eval_sqlite(path: Path, items: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE eval_items (
              id TEXT PRIMARY KEY,
              task_type TEXT NOT NULL,
              source_record_type TEXT NOT NULL,
              source_id TEXT NOT NULL,
              source_video_id TEXT,
              source_video_ids_json TEXT NOT NULL,
              start_s REAL,
              end_s REAL,
              time_label TEXT,
              prompt TEXT,
              predicted_label TEXT,
              predicted_summary TEXT,
              confidence REAL,
              review_status TEXT,
              reachout_suggested INTEGER,
              family_followup_question TEXT,
              predicted_fields_json TEXT NOT NULL,
              evidence_summary TEXT,
              reviewer_guidance_json TEXT NOT NULL,
              tags_json TEXT NOT NULL
            );
            CREATE TABLE annotation_template (
              eval_item_id TEXT PRIMARY KEY,
              judgment TEXT,
              corrected_value TEXT,
              needs_family_followup INTEGER,
              family_followup_question TEXT,
              notes TEXT,
              reviewer TEXT,
              reviewed_at TEXT
            );
            CREATE TABLE judgment_options (
              judgment TEXT PRIMARY KEY,
              scored INTEGER NOT NULL,
              score REAL,
              guidance TEXT NOT NULL
            );
            """
        )
        judgment_guidance = {
            "correct": "Prediction is usable as-is.",
            "mostly_correct": "Main idea is right; minor cleanup may be needed.",
            "partial": "Record is useful but has meaningful missing or wrong details.",
            "incorrect": "Prediction is wrong or misleading.",
            "needs_followup": "Reviewer needs to ask another family member.",
            "not_sure": "Reviewer cannot judge yet.",
            "skip": "Do not use this annotation in scoring.",
        }
        for judgment in [*JUDGMENT_SCORES, *OPEN_JUDGMENTS]:
            conn.execute(
                """
                INSERT INTO judgment_options (judgment, scored, score, guidance)
                VALUES (?, ?, ?, ?)
                """,
                (
                    judgment,
                    1 if judgment in JUDGMENT_SCORES else 0,
                    JUDGMENT_SCORES.get(judgment),
                    judgment_guidance[judgment],
                ),
            )
        for item in items:
            conn.execute(
                """
                INSERT INTO eval_items (
                  id, task_type, source_record_type, source_id, source_video_id,
                  source_video_ids_json, start_s, end_s, time_label, prompt, predicted_label,
                  predicted_summary, confidence, review_status, reachout_suggested,
                  family_followup_question, predicted_fields_json, evidence_summary,
                  reviewer_guidance_json, tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"],
                    item["task_type"],
                    item["source_record_type"],
                    item["source_id"],
                    item.get("source_video_id") or "",
                    json.dumps(item.get("source_video_ids") or [], sort_keys=True),
                    _number_or_none(item.get("start_s")),
                    _number_or_none(item.get("end_s")),
                    item.get("time_label") or "",
                    item.get("prompt") or "",
                    item.get("predicted_label") or "",
                    item.get("predicted_summary") or "",
                    _number_or_none(item.get("confidence")),
                    item.get("review_status") or "",
                    1 if item.get("reachout_suggested") else 0,
                    item.get("family_followup_question") or "",
                    json.dumps(item.get("predicted_fields") or {}, sort_keys=True),
                    item.get("evidence_summary") or "",
                    json.dumps(item.get("reviewer_guidance") or [], sort_keys=True),
                    json.dumps(item.get("tags") or [], sort_keys=True),
                ),
            )
        for annotation in annotations:
            conn.execute(
                """
                INSERT INTO annotation_template (
                  eval_item_id, judgment, corrected_value, needs_family_followup,
                  family_followup_question, notes, reviewer, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation["eval_item_id"],
                    annotation["judgment"],
                    annotation["corrected_value"],
                    1 if annotation["needs_family_followup"] else 0,
                    annotation["family_followup_question"],
                    annotation["notes"],
                    annotation["reviewer"],
                    annotation["reviewed_at"],
                ),
            )
        conn.executescript(
            """
            CREATE VIEW task_counts AS
            SELECT task_type, COUNT(*) AS item_count
            FROM eval_items
            GROUP BY task_type
            ORDER BY item_count DESC, task_type;

            CREATE VIEW followup_queue AS
            SELECT id, task_type, predicted_label, time_label, family_followup_question,
                   evidence_summary
            FROM eval_items
            WHERE reachout_suggested = 1
            ORDER BY task_type, start_s, id;

            CREATE VIEW low_confidence_queue AS
            SELECT id, task_type, predicted_label, confidence, review_status, prompt
            FROM eval_items
            WHERE confidence IS NULL OR confidence < 0.75 OR review_status = 'needs_review'
            ORDER BY confidence IS NULL DESC, confidence ASC, task_type, id;

            CREATE VIEW prompting_lab AS
            SELECT id, task_type, prompt, predicted_fields_json, evidence_summary
            FROM eval_items
            ORDER BY task_type, start_s, id;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _write_score_sqlite(path: Path, scored_items: list[dict[str, Any]], report: dict[str, Any]) -> None:
    if not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS scored_annotations;
            DROP TABLE IF EXISTS score_summary;
            CREATE TABLE scored_annotations (
              eval_item_id TEXT PRIMARY KEY,
              task_type TEXT NOT NULL,
              source_record_type TEXT,
              source_id TEXT,
              predicted_label TEXT,
              judgment TEXT NOT NULL,
              score REAL,
              needs_family_followup INTEGER NOT NULL,
              family_followup_question TEXT,
              corrected_value TEXT,
              notes TEXT,
              reviewer TEXT,
              reviewed_at TEXT
            );
            CREATE TABLE score_summary (
              scope TEXT NOT NULL,
              task_type TEXT,
              total INTEGER NOT NULL,
              scored INTEGER NOT NULL,
              score REAL,
              judgments_json TEXT NOT NULL
            );
            """
        )
        for item in scored_items:
            conn.execute(
                """
                INSERT INTO scored_annotations (
                  eval_item_id, task_type, source_record_type, source_id,
                  predicted_label, judgment, score, needs_family_followup,
                  family_followup_question, corrected_value, notes, reviewer, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["eval_item_id"],
                    item["task_type"],
                    item.get("source_record_type") or "",
                    item.get("source_id") or "",
                    item.get("predicted_label") or "",
                    item["judgment"],
                    item.get("score"),
                    1 if item.get("needs_family_followup") else 0,
                    item.get("family_followup_question") or "",
                    item.get("corrected_value") or "",
                    item.get("notes") or "",
                    item.get("reviewer") or "",
                    item.get("reviewed_at") or "",
                ),
            )
        _insert_score_summary(conn, "overall", None, report["overall"])
        for task_type, metric in report["by_task_type"].items():
            _insert_score_summary(conn, "task_type", task_type, metric)
        conn.commit()
    finally:
        conn.close()


def _insert_score_summary(
    conn: sqlite3.Connection,
    scope: str,
    task_type: str | None,
    metric: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO score_summary (scope, task_type, total, scored, score, judgments_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scope,
            task_type,
            int(metric["total"]),
            int(metric["scored"]),
            metric.get("score"),
            json.dumps(metric.get("judgments") or {}, sort_keys=True),
        ),
    )


def _write_reviewer_readme(path: Path, project: Path, items: list[dict[str, Any]]) -> None:
    by_task = Counter(item["task_type"] for item in items)
    path.write_text(
        f"""# tapesplit Evaluation Packet

Project: `{project}`

This packet is for a family reviewer who can verify events, people, places, dates,
and relationships. It is also a small SQL/data-science project.

## Files

- `eval_items.jsonl`: model/system predictions to review.
- `eval_items.csv`: same items in spreadsheet-friendly form.
- `annotations.template.jsonl`: copy this to `annotations.jsonl` and fill it in.
- `annotations.template.csv`: spreadsheet-friendly annotation template.
- `eval.sqlite`: local SQLite database for SQL practice.
- `eval_manifest.json`: packet summary.

## Item Counts

{_markdown_counts(by_task)}

## Judgments

Use one of:

```text
correct
mostly_correct
partial
incorrect
not_sure
needs_followup
skip
```

Use `needs_followup` when you need to ask another family member. Keep the
question in `family_followup_question` so it becomes a to-do queue.

The easiest non-technical review path is to copy `annotations.template.csv` to
`annotations.csv`, fill the `judgment`, `corrected_value`, `notes`, and
`reviewer` columns, then run:

```bash
tapesplit eval score "{project}" --annotations annotations.csv
```

## Suggested SQL Exercises

```sql
-- Count tasks by type
SELECT task_type, COUNT(*) AS n
FROM eval_items
GROUP BY task_type
ORDER BY n DESC;

-- The same question using the built-in view
SELECT * FROM task_counts;

-- Find likely family follow-up questions
SELECT id, task_type, predicted_label, family_followup_question
FROM eval_items
WHERE reachout_suggested = 1
ORDER BY task_type, start_s;

-- Review all place-resolution tasks in timeline order
SELECT id, time_label, predicted_label, predicted_summary
FROM eval_items
WHERE task_type = 'place_resolution'
ORDER BY start_s;

-- Find lower-confidence items first
SELECT id, task_type, predicted_label, confidence
FROM eval_items
WHERE confidence IS NOT NULL
ORDER BY confidence ASC
LIMIT 20;

-- Compare human judgments after running `tapesplit eval score`
SELECT task_type, judgment, COUNT(*) AS n
FROM scored_annotations
GROUP BY task_type, judgment
ORDER BY task_type, n DESC;
```

## Prompting Exercise

For any difficult item, copy `prompt`, `predicted_fields_json`, and
`evidence_summary` into an LLM and ask it:

```text
Given only this evidence, what should a reviewer verify, and what question should
we ask a family member?
```

Then compare the model's answer with the human annotation.
""",
        encoding="utf-8",
    )


def _write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    columns = [
        "id",
        "task_type",
        "source_record_type",
        "source_id",
        "source_video_id",
        "time_label",
        "prompt",
        "predicted_label",
        "predicted_summary",
        "confidence",
        "review_status",
        "reachout_suggested",
        "family_followup_question",
        "evidence_summary",
        "predicted_fields_json",
        "reviewer_guidance_json",
        "tags",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    **{key: item.get(key) for key in columns},
                    "predicted_fields_json": json.dumps(item.get("predicted_fields") or {}, sort_keys=True),
                    "reviewer_guidance_json": json.dumps(item.get("reviewer_guidance") or [], sort_keys=True),
                    "tags": ", ".join(item.get("tags") or []),
                }
            )


def _write_annotation_csv(path: Path, annotations: list[dict[str, Any]]) -> None:
    columns = [
        "eval_item_id",
        "judgment",
        "corrected_value",
        "needs_family_followup",
        "family_followup_question",
        "notes",
        "reviewer",
        "reviewed_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for annotation in annotations:
            writer.writerow({key: annotation.get(key, "") for key in columns})


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)
    if not rows:
        path.write_text("", encoding="utf-8")


def _metric_bucket() -> dict[str, Any]:
    return {"total": 0, "scored": 0, "score_sum": 0.0, "judgments": Counter()}


def _add_metric(bucket: dict[str, Any], judgment: str, score: float | None) -> None:
    bucket["total"] += 1
    bucket["judgments"][judgment] += 1
    if score is not None:
        bucket["scored"] += 1
        bucket["score_sum"] += score


def _finalize_metric(bucket: dict[str, Any]) -> dict[str, Any]:
    scored = int(bucket["scored"])
    return {
        "total": int(bucket["total"]),
        "scored": scored,
        "score": round(float(bucket["score_sum"]) / scored, 3) if scored else None,
        "judgments": dict(bucket["judgments"]),
    }


def _normalize_judgment(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return text if text in {*JUDGMENT_SCORES, *OPEN_JUDGMENTS} else "not_sure"


def _read_annotations(path: Path) -> list[dict[str, Any]]:
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    return read_jsonl(path)


def _resolve_annotations_file(packet: Path, annotations_path: Path | None) -> Path:
    if annotations_path is not None:
        path = annotations_path.expanduser()
        return path.resolve() if path.is_absolute() else (packet / path).resolve()
    jsonl_path = packet / "annotations.jsonl"
    if jsonl_path.exists():
        return jsonl_path
    csv_path = packet / "annotations.csv"
    if csv_path.exists():
        return csv_path
    return jsonl_path


def _resolve_eval_dir(project: Path, eval_dir: Path | None) -> Path:
    if eval_dir is None:
        return (project / "eval_packet").resolve()
    path = eval_dir.expanduser()
    return path.resolve() if path.is_absolute() else (project / path).resolve()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _source_video_ids(row: dict[str, Any], evidence_ids: list[str], evidence_by_id: dict[str, dict[str, Any]]) -> list[str]:
    values = []
    for key in ["source_video_id", "source_video_ids"]:
        value = row.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item)
        elif value:
            values.append(str(value))
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    if isinstance(scope.get("source_video_ids"), list):
        values.extend(str(item) for item in scope["source_video_ids"] if item)
    for evidence_id in evidence_ids:
        source_video_id = evidence_by_id.get(evidence_id, {}).get("source_video_id")
        if source_video_id:
            values.append(str(source_video_id))
    return _unique_items(values)


def _visible_event_ids(project: Path, visibility: Any, evidence_by_id: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(row["id"])
        for row in read_jsonl(project / "canonical_events.jsonl")
        if row.get("id") and visibility.visible_row(row, evidence_by_id=evidence_by_id)
    }


def _all_event_ids(project: Path) -> set[str]:
    return {str(row["id"]) for row in read_jsonl(project / "canonical_events.jsonl") if row.get("id")}


def _eval_item_has_visible_event_context(
    item: dict[str, Any],
    *,
    all_event_ids: set[str],
    visible_event_ids: set[str],
) -> bool:
    event_ids = set(_event_ids_from_eval_item(item))
    if not event_ids:
        return True
    if not all_event_ids:
        return True
    return bool(event_ids & visible_event_ids)


def _event_ids_from_eval_item(item: dict[str, Any]) -> list[str]:
    values = []
    fields = item.get("predicted_fields") if isinstance(item.get("predicted_fields"), dict) else {}
    values.extend(_list_values(fields.get("canonical_event_ids")))
    values.extend(_list_values(fields.get("canonical_event_id")))
    scope = fields.get("scope") if isinstance(fields.get("scope"), dict) else {}
    values.extend(_list_values(scope.get("canonical_event_ids")))
    return _unique_items(values)


def _list_values(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value] if value not in (None, "") else []


def _evidence_summary(evidence_ids: list[str], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    parts = []
    for evidence_id in evidence_ids[:5]:
        evidence = evidence_by_id.get(evidence_id, {})
        if not evidence:
            continue
        text = " ".join(str(evidence.get("text") or "").split())
        label = str(evidence.get("kind") or evidence.get("predicate") or evidence_id)
        time_label = _range_label(evidence.get("start_s"), evidence.get("end_s"))
        parts.append(f"{evidence_id} {label} {time_label}: {text[:180]}")
    return " | ".join(parts)


def _scope_value(row: dict[str, Any], key: str) -> Any:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    return scope.get(key)


def _eval_item_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    priority = {
        "event_correctness": 0,
        "album_grouping": 1,
        "place_resolution": 2,
        "person_identity": 3,
        "date_candidate": 4,
        "relationship_candidate": 5,
        "context_edge": 6,
    }
    return (
        priority.get(str(item.get("task_type")), 99),
        _number_or_large(item.get("start_s")),
        str(item.get("source_id") or ""),
    )


def _default_followup_question(task_type: str, predicted_label: Any) -> str:
    label = str(predicted_label or "this item")
    if task_type == "place_resolution":
        return f"Do you recognize where '{label}' is, or what city/place name it should have?"
    if task_type == "person_identity":
        return f"Who is '{label}', and are all listed aliases the same person?"
    if task_type == "relationship_candidate":
        return f"Can someone confirm the relationship claim: {label}?"
    if task_type == "date_candidate":
        return f"Can someone confirm the date for '{label}'?"
    return f"Can someone verify '{label}'?"


def _markdown_counts(counts: Counter[str]) -> str:
    if not counts:
        return "- none"
    return "\n".join(f"- `{key}`: {value}" for key, value in sorted(counts.items()))


def _unique_items(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _range_label(start_s: Any, end_s: Any) -> str:
    start = _format_seconds(start_s)
    end = _format_seconds(end_s)
    if start and end:
        return f"{start}-{end}"
    return start or end or "unknown time"


def _format_seconds(value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return ""
    seconds = max(0, int(round(number)))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

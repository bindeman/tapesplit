"""v2 claim substrate (REFOUNDATION.md section 2, module M1).

One store for every assertion the pipeline makes. Claims carry producer
provenance, a span with an explicit reference frame, confidence, and
verification status. v1 artifacts stay authoritative during the strangler
cutover; producers dual-write claims alongside them, and the oracle diff
(`diff_v1_artifact`) proves the v1 view is regenerable from claims.

Reference-frame rules are the design's centerpiece: a span whose timestamps
violate its clock's bounds is rejected at write time — never silently
clamped. Nothing is deleted, only superseded.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapesplit.media import build_media_index
from tapesplit.storage import read_jsonl

CLAIM_STORE_FILENAME = "claim_store.sqlite3"
CLAIM_EXPORT_FILENAME = "claim_store_export.jsonl"

CLAIM_KINDS = {
    "event",
    "place_link",
    "date",
    "identity",
    "relationship",
    "presence",
    "human_action",
}
SPAN_CLOCKS = {"source", "chunk", "capture"}
VERIFICATION_STATUSES = {"unverified", "supported", "contradicted", "undecidable"}

# Float slop tolerated when checking a span against its clock's bound; real
# overruns are orders of magnitude larger (the #13 chunk drift reached 1.67x).
SPAN_BOUND_EPSILON_S = 0.001

_DISABLE_VALUES = {"0", "false", "off", "no"}


class SpanValidationError(ValueError):
    """A span violates its declared reference frame; the claim is rejected."""


@dataclass(frozen=True)
class Span:
    clock: str
    start_s: float
    end_s: float


def claims_enabled() -> bool:
    return os.environ.get("TAPESPLIT_CLAIMS", "").strip().lower() not in _DISABLE_VALUES


def validate_span(
    span: Span,
    *,
    chunk_duration_s: float | None = None,
    media_duration_s: float | None = None,
) -> None:
    """Reject spans that violate their reference frame. Never clamps."""

    if span.clock not in SPAN_CLOCKS:
        raise SpanValidationError(f"unknown span clock: {span.clock!r}")
    for label, value in (("start_s", span.start_s), ("end_s", span.end_s)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpanValidationError(f"span {label} must be a number, got {value!r}")
        if value < 0:
            raise SpanValidationError(f"span {label} must be non-negative, got {value}")
    if span.end_s < span.start_s:
        raise SpanValidationError(f"span end_s {span.end_s} precedes start_s {span.start_s}")
    if span.clock == "chunk":
        if chunk_duration_s is None:
            raise SpanValidationError("chunk-clock span requires chunk_duration_s")
        if span.end_s > chunk_duration_s + SPAN_BOUND_EPSILON_S:
            raise SpanValidationError(
                f"chunk-clock span end_s {span.end_s} exceeds chunk duration {chunk_duration_s}"
            )
    if span.clock == "source" and media_duration_s is not None:
        if span.end_s > media_duration_s + SPAN_BOUND_EPSILON_S:
            raise SpanValidationError(
                f"source-clock span end_s {span.end_s} exceeds media duration {media_duration_s}"
            )


def chunk_span_to_source(span: Span, *, chunk_offset_s: float, chunk_duration_s: float) -> tuple[Span, float]:
    """Explicit chunk->source conversion; returns the new span and the applied offset."""

    if span.clock != "chunk":
        raise SpanValidationError(f"chunk_span_to_source requires a chunk-clock span, got {span.clock!r}")
    validate_span(span, chunk_duration_s=chunk_duration_s)
    converted = Span("source", span.start_s + chunk_offset_s, span.end_s + chunk_offset_s)
    return converted, chunk_offset_s


_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    rowid_alias INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    media_id TEXT,
    clock TEXT,
    start_s REAL,
    end_s REAL,
    producer TEXT NOT NULL,
    run_id TEXT,
    chunk_id TEXT,
    confidence REAL,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    superseded_by TEXT,
    v1_artifact TEXT,
    v1_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_kind ON claims(kind);
CREATE INDEX IF NOT EXISTS idx_claims_media ON claims(media_id);
CREATE INDEX IF NOT EXISTS idx_claims_artifact ON claims(v1_artifact, v1_id);
CREATE INDEX IF NOT EXISTS idx_claims_superseded ON claims(superseded_by);
"""


class ClaimStore:
    """SQLite-backed claim store living inside the project directory."""

    def __init__(self, project_dir: Path):
        self.project = project_dir.expanduser().resolve()
        self.path = self.project / CLAIM_STORE_FILENAME
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ClaimStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def add_claim(
        self,
        kind: str,
        *,
        producer: str,
        assertion: dict[str, Any] | None = None,
        media_id: str | None = None,
        span: Span | None = None,
        chunk_id: str | None = None,
        chunk_offset_s: float | None = None,
        chunk_duration_s: float | None = None,
        media_duration_s: float | None = None,
        run_id: str | None = None,
        confidence: float | None = None,
        derived_from: list[str] | None = None,
        v1_artifact: str | None = None,
        v1_id: str | None = None,
        v1_row: dict[str, Any] | None = None,
        rejection: dict[str, Any] | None = None,
        verification_status: str = "unverified",
    ) -> str:
        if kind not in CLAIM_KINDS:
            raise ValueError(f"unknown claim kind: {kind!r}")
        if verification_status not in VERIFICATION_STATUSES:
            raise ValueError(f"unknown verification status: {verification_status!r}")
        if not producer:
            raise ValueError("claim requires a producer")
        if span is not None:
            validate_span(span, chunk_duration_s=chunk_duration_s, media_duration_s=media_duration_s)

        payload = {
            "assertion": assertion or {},
            "provenance": {
                "producer": producer,
                "run_id": run_id,
                "request_scope": _request_scope(chunk_id, chunk_offset_s, chunk_duration_s),
                "derived_from": derived_from or [],
            },
            "subject": {
                "media_id": media_id,
                "span": {"clock": span.clock, "start_s": span.start_s, "end_s": span.end_s} if span else None,
            },
        }
        if v1_row is not None:
            payload["v1_row"] = v1_row
        if rejection is not None:
            payload["rejection"] = rejection

        cursor = self._conn.execute(
            "INSERT INTO claims (id, kind, media_id, clock, start_s, end_s, producer, run_id,"
            " chunk_id, confidence, verification_status, superseded_by, v1_artifact, v1_id,"
            " payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                "pending",
                kind,
                media_id,
                span.clock if span else None,
                span.start_s if span else None,
                span.end_s if span else None,
                producer,
                run_id,
                chunk_id,
                confidence,
                verification_status,
                v1_artifact,
                v1_id,
                json.dumps(payload, sort_keys=True),
                _now_iso(),
            ),
        )
        claim_id = f"claim_{cursor.lastrowid:08d}"
        self._conn.execute("UPDATE claims SET id = ? WHERE rowid_alias = ?", (claim_id, cursor.lastrowid))
        self._conn.commit()
        return claim_id

    def supersede_artifact(self, v1_artifact: str, reason: str) -> int:
        """Mark all live claims for a regenerated v1 artifact superseded. Nothing is deleted."""

        cursor = self._conn.execute(
            "UPDATE claims SET superseded_by = ? WHERE v1_artifact = ? AND superseded_by IS NULL",
            (reason, v1_artifact),
        )
        self._conn.commit()
        return cursor.rowcount

    def supersede_claim(self, claim_id: str, *, by: str) -> None:
        self._conn.execute(
            "UPDATE claims SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
            (by, claim_id),
        )
        self._conn.commit()

    def claims(
        self,
        *,
        kind: str | None = None,
        media_id: str | None = None,
        v1_artifact: str | None = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM claims"
        clauses, params = [], []
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        for column, value in (("kind", kind), ("media_id", media_id), ("v1_artifact", v1_artifact)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid_alias"
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(query, params).fetchall()
        self._conn.row_factory = None
        return [_decode_row(row) for row in rows]

    def existing_v1_ids(self, v1_artifact: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT v1_id FROM claims WHERE v1_artifact = ? AND v1_id IS NOT NULL",
            (v1_artifact,),
        ).fetchall()
        return {row[0] for row in rows}

    def stats(self) -> dict[str, Any]:
        def count_by(column: str) -> dict[str, int]:
            rows = self._conn.execute(
                f"SELECT {column}, COUNT(*) FROM claims WHERE superseded_by IS NULL GROUP BY {column}"
            ).fetchall()
            return {str(key): count for key, count in rows}

        total = self._conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        superseded = self._conn.execute(
            "SELECT COUNT(*) FROM claims WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]
        return {
            "store": str(self.path),
            "claims_total": total,
            "claims_live": total - superseded,
            "claims_superseded": superseded,
            "by_kind": count_by("kind"),
            "by_producer": count_by("producer"),
            "by_verification_status": count_by("verification_status"),
            "by_v1_artifact": count_by("v1_artifact"),
        }

    def export_jsonl(self, path: Path | None = None) -> dict[str, Any]:
        output = path or (self.project / CLAIM_EXPORT_FILENAME)
        claims = self.claims(include_superseded=True)
        with output.open("w", encoding="utf-8") as handle:
            for claim in claims:
                handle.write(json.dumps(claim, sort_keys=True) + "\n")
        return {"output": str(output), "claims_exported": len(claims)}


class DualWriter:
    """Best-effort claim mirror for a v1 artifact writer.

    v1 behavior must be unchanged whether claims are enabled, disabled, or
    failing: every operation is wrapped, and the first error disables the
    writer for the rest of the run (recorded in the summary, never raised).
    """

    def __init__(self, store: ClaimStore | None, artifact: str, producer: str, run_id: str | None):
        self._store = store
        self.artifact = artifact
        self.producer = producer
        self.run_id = run_id
        self.claims_written = 0
        self.rejections_recorded = 0
        self.error: str | None = None

    @classmethod
    def open(cls, project_dir: Path, *, artifact: str, producer: str, run_id: str | None = None) -> "DualWriter":
        if not claims_enabled():
            return cls(None, artifact, producer, run_id)
        try:
            store = ClaimStore(project_dir)
            build_media_index(project_dir)
            return cls(store, artifact, producer, run_id)
        except Exception as error:  # pragma: no cover - defensive: dual-write must never break v1
            writer = cls(None, artifact, producer, run_id)
            writer.error = f"claim store unavailable: {error}"
            return writer

    def _guard(self) -> bool:
        return self._store is not None and self.error is None

    def supersede_previous(self, reason: str | None = None) -> None:
        if not self._guard():
            return
        try:
            self._store.supersede_artifact(self.artifact, reason or f"regenerated:{self.run_id or 'unknown'}")
        except Exception as error:
            self.error = str(error)

    def write_row(
        self,
        row: dict[str, Any],
        *,
        kind: str,
        media_id: str | None,
        start_s: float | None = None,
        end_s: float | None = None,
        confidence: float | None = None,
        chunk: dict[str, Any] | None = None,
        media_duration_s: float | None = None,
        assertion: dict[str, Any] | None = None,
        producer: str | None = None,
    ) -> None:
        if not self._guard():
            return
        try:
            span = None
            if start_s is not None and end_s is not None:
                span = Span("source", float(start_s), float(end_s))
            chunk = chunk or {}
            self._store.add_claim(
                kind,
                producer=producer or self.producer,
                assertion=assertion or {"title": row.get("title"), "summary": row.get("summary")},
                media_id=media_id,
                span=span,
                chunk_id=_string_or_none(chunk.get("chunk_id")),
                chunk_offset_s=_number_or_none(chunk.get("chunk_start_s")),
                chunk_duration_s=_number_or_none(chunk.get("chunk_duration_s")),
                media_duration_s=media_duration_s,
                run_id=self.run_id,
                confidence=_number_or_none(confidence),
                v1_artifact=self.artifact,
                v1_id=_string_or_none(row.get("id")),
                v1_row=row,
            )
            self.claims_written += 1
        except Exception as error:
            self.error = str(error)

    def reject(self, item: dict[str, Any], *, kind: str = "event", media_id: str | None = None, reason: str | None = None) -> None:
        """Record a rejected candidate as a claim; silent drops are v1 history."""

        if not self._guard():
            return
        try:
            self._store.add_claim(
                kind,
                producer=self.producer,
                assertion={"title": (item.get("item") or {}).get("title") if isinstance(item.get("item"), dict) else item.get("title")},
                media_id=media_id,
                run_id=self.run_id,
                v1_artifact=self.artifact,
                rejection={"reason": reason or item.get("reason") or "skipped", "item": item},
            )
            self.rejections_recorded += 1
        except Exception as error:
            self.error = str(error)

    def summary(self) -> dict[str, Any]:
        if self._store is None and self.error is None:
            return {"enabled": False}
        result: dict[str, Any] = {
            "enabled": True,
            "claims_written": self.claims_written,
            "rejections_recorded": self.rejections_recorded,
        }
        if self.error:
            result["error"] = self.error
        return result

    def close(self) -> None:
        if self._store is not None:
            self._store.close()


def diff_v1_artifact(project_dir: Path, artifact: str) -> dict[str, Any]:
    """Oracle diff: is the v1 artifact regenerable from live claims, byte for byte?"""

    project = project_dir.expanduser().resolve()
    artifact_path = project / artifact
    v1_rows = read_jsonl(artifact_path)
    v1_by_id = {str(row.get("id")): row for row in v1_rows if row.get("id")}

    with ClaimStore(project) as store:
        claims = store.claims(v1_artifact=artifact)
    claim_rows = {}
    for claim in claims:
        v1_row = (claim.get("payload") or {}).get("v1_row")
        if claim.get("v1_id") and v1_row is not None:
            claim_rows[str(claim["v1_id"])] = v1_row

    missing_in_claims = sorted(set(v1_by_id) - set(claim_rows))
    extra_in_claims = sorted(set(claim_rows) - set(v1_by_id))
    mismatched = []
    for v1_id in sorted(set(v1_by_id) & set(claim_rows)):
        if json.dumps(v1_by_id[v1_id], sort_keys=True) != json.dumps(claim_rows[v1_id], sort_keys=True):
            mismatched.append(v1_id)

    reconstructed = "".join(
        json.dumps(claim_rows[str(row.get("id"))], sort_keys=True) + "\n"
        for row in v1_rows
        if str(row.get("id")) in claim_rows
    )
    actual = "".join(json.dumps(row, sort_keys=True) + "\n" for row in v1_rows)
    return {
        "artifact": artifact,
        "v1_rows": len(v1_rows),
        "claim_rows": len(claim_rows),
        "identical": len(v1_by_id) - len(missing_in_claims) - len(mismatched),
        "missing_in_claims": missing_in_claims,
        "extra_in_claims": extra_in_claims,
        "mismatched": mismatched,
        "byte_identical": not missing_in_claims and not extra_in_claims and not mismatched and reconstructed == actual,
    }


def backfill_project_claims(project_dir: Path) -> dict[str, Any]:
    """Write claims for existing v1 rows that predate the substrate (idempotent)."""

    project = project_dir.expanduser().resolve()
    build_media_index(project)
    written = {}
    with ClaimStore(project) as store:
        for artifact, kind, producer_key in (
            ("gemini_events.jsonl", "event", "source"),
            ("heuristic_events.jsonl", "event", "source"),
            ("corrections.jsonl", "human_action", "reviewer"),
        ):
            rows = read_jsonl(project / artifact)
            existing = store.existing_v1_ids(artifact)
            count = 0
            for row in rows:
                row_id = _string_or_none(row.get("id"))
                if row_id is None or row_id in existing:
                    continue
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                store.add_claim(
                    kind,
                    producer=str(row.get(producer_key) or "unknown"),
                    assertion={"title": row.get("title"), "action": row.get("action")},
                    media_id=_string_or_none(row.get("source_video_id") or row.get("source_media_id")),
                    span=_row_span(row),
                    chunk_id=_string_or_none(metadata.get("chunk_id")),
                    chunk_offset_s=_number_or_none(metadata.get("chunk_start_s")),
                    chunk_duration_s=_number_or_none(metadata.get("chunk_duration_s")),
                    confidence=_number_or_none(row.get("confidence")),
                    v1_artifact=artifact,
                    v1_id=row_id,
                    v1_row=row,
                )
                count += 1
            written[artifact] = count
    return {"project": str(project), "backfilled": written}


def _row_span(row: dict[str, Any]) -> Span | None:
    start_s = _number_or_none(row.get("start_s"))
    end_s = _number_or_none(row.get("end_s"))
    if start_s is None or end_s is None:
        return None
    return Span("source", start_s, end_s)


def _request_scope(chunk_id: str | None, chunk_offset_s: float | None, chunk_duration_s: float | None) -> dict[str, Any]:
    if chunk_id is None and chunk_offset_s is None:
        return {"kind": "whole"}
    return {
        "kind": "chunk",
        "chunk_id": chunk_id,
        "chunk_offset_s": chunk_offset_s,
        "chunk_duration_s": chunk_duration_s,
    }


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    record = {key: row[key] for key in row.keys() if key != "rowid_alias"}
    record["payload"] = json.loads(record["payload"])
    return record


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

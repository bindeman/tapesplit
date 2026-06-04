from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from tapesplit.storage import append_jsonl, read_jsonl


DATE_FIELDS = [
    "CreateDate",
    "CreationDate",
    "ModifyDate",
    "TrackCreateDate",
    "TrackModifyDate",
    "MediaCreateDate",
    "MediaModifyDate",
    "FileModifyDate",
]


def extract_exif_for_project(project_dir: Path) -> dict:
    project = project_dir.expanduser().resolve()
    output_path = project / "media_metadata.jsonl"
    if output_path.exists():
        output_path.unlink()

    rows = read_jsonl(project / "tapes.jsonl")
    records = []
    for row in rows:
        metadata = load_exif_metadata(Path(row["path"]))
        record = {
            "source_video_id": row["id"],
            "filename": row["filename"],
            "metadata": metadata,
            "date_candidates": extract_date_candidates(metadata),
        }
        append_jsonl(output_path, record)
        records.append(record)

    return {
        "project": str(project),
        "records": len(records),
        "output": str(output_path),
        "date_candidates": [
            {
                "source_video_id": record["source_video_id"],
                "filename": record["filename"],
                "date_candidates": record["date_candidates"],
            }
            for record in records
        ],
    }


def load_exif_metadata(path: Path) -> dict[str, Any]:
    cmd = ["exiftool", "-json", "-n", str(path)]
    try:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("exiftool is required. Install it with: brew install exiftool") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"exiftool failed for {path}: {detail}") from exc

    payload = json.loads(completed.stdout)
    if not payload:
        return {}
    return payload[0]


def extract_date_candidates(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for field in DATE_FIELDS:
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        parsed = parse_exif_date(value)
        candidates.append(
            {
                "field": field,
                "raw": value,
                "iso": parsed,
                "confidence": _date_field_confidence(field),
                "note": _date_field_note(field),
            }
        )
    return candidates


def parse_exif_date(value: str) -> str | None:
    cleaned = value.strip()
    match = re.match(
        r"^(?P<year>\d{4}):(?P<month>\d{2}):(?P<day>\d{2})"
        r"(?: (?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}))?"
        r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$",
        cleaned,
    )
    if not match:
        return None
    parts = match.groupdict()
    if parts["year"] == "0000" or parts["month"] == "00" or parts["day"] == "00":
        return None
    date_part = f"{parts['year']}-{parts['month']}-{parts['day']}"
    if parts.get("hour") is None:
        return date_part
    time_part = f"{parts['hour']}:{parts['minute']}:{parts['second']}"
    tz = parts.get("tz")
    if tz and tz != "Z" and ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    return f"{date_part}T{time_part}{tz or ''}"


def _date_field_confidence(field: str) -> float:
    if field in {"CreateDate", "CreationDate"}:
        return 0.65
    if field in {"TrackCreateDate", "MediaCreateDate"}:
        return 0.55
    if field == "FileModifyDate":
        return 0.35
    return 0.3


def _date_field_note(field: str) -> str:
    if field in {"CreateDate", "CreationDate", "TrackCreateDate", "MediaCreateDate"}:
        return "Container date; may be digitization/export date rather than original recording date."
    if field == "FileModifyDate":
        return "Filesystem date; usually weak evidence for original recording date."
    return "Metadata date; needs corroboration."


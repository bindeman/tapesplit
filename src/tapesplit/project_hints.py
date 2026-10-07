"""Per-archive hints: facts about one family's tapes, not about home video.

Anything that is true only of a particular archive (who usually held the
camera, an extra place alias) belongs in ``<project>/project_hints.json``,
never in the codebase. Every key is optional and a missing file means no
hints, so a fresh archive behaves generically.

Recognized keys:

- ``camera_operator``: a person label or alias from ``people_groups.jsonl``.
  Speaker identity offers it as a low-confidence hypothesis for the recurring
  unnamed adult voice (the classic camera-operator profile: talks a lot, is
  rarely on screen). Without it that voice stays unattributed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_HINTS_FILENAME = "project_hints.json"


def load_project_hints(project_dir: Path) -> dict[str, Any]:
    path = Path(project_dir) / PROJECT_HINTS_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

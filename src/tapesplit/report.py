from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from tapesplit.costs import summarize_project_costs
from tapesplit.storage import read_jsonl


def export_review_report(project_dir: Path) -> dict:
    project = project_dir.expanduser().resolve()
    evidence = read_jsonl(project / "evidence.jsonl")
    claims = read_jsonl(project / "claims.jsonl")
    events = read_jsonl(project / "events.jsonl")
    summaries = read_jsonl(project / "summaries.jsonl")
    non_content = read_jsonl(project / "non_content_ranges.jsonl")
    costs = summarize_project_costs(project)
    output = project / "review.html"
    output.write_text(
        _render_html(
            evidence=evidence,
            claims=claims,
            events=events,
            summaries=summaries,
            non_content=non_content,
            costs=costs,
        ),
        encoding="utf-8",
    )
    return {
        "project": str(project),
        "output": str(output),
        "evidence": len(evidence),
        "claims": len(claims),
        "events": len(events),
    }


def _render_html(
    *,
    evidence: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    events: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    non_content: list[dict[str, Any]],
    costs: dict[str, Any],
) -> str:
    summary_text = "\n".join(item.get("text", "") for item in summaries if item.get("text"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TapeSplit Review</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
    h1, h2 {{ margin: 0 0 12px; }}
    section {{ margin: 28px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #d9e2ec; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f4f8; }}
    .muted {{ color: #627d98; }}
    .pill {{ display: inline-block; padding: 2px 6px; border-radius: 6px; background: #e6f6ff; }}
    pre {{ white-space: pre-wrap; background: #f5f7fa; padding: 12px; border-radius: 8px; }}
  </style>
</head>
<body>
  <h1>TapeSplit Review</h1>
  <p class="muted">Generated from local evidence, TwelveLabs search output, and Azure extraction claims.</p>

  <section>
    <h2>Summary</h2>
    <pre>{html.escape(summary_text or "No summary generated yet.")}</pre>
  </section>

  <section>
    <h2>Cost</h2>
    <p><strong>Estimated total:</strong> ${costs.get("estimated_total_cost_usd", 0):.6f}</p>
    <p class="muted">Unknown-cost records: {costs.get("unknown_cost_records", 0)}</p>
  </section>

  <section>
    <h2>Event Candidates</h2>
    {_table(events, ["id", "title", "start_s", "end_s", "confidence", "evidence_ids", "summary"])}
  </section>

  <section>
    <h2>Claims</h2>
    {_table(claims, ["id", "predicate", "value", "confidence", "evidence_ids", "notes"])}
  </section>

  <section>
    <h2>Non-Content Ranges</h2>
    {_table(non_content, ["label", "start_s", "end_s", "duration_s", "confidence"])}
  </section>

  <section>
    <h2>Evidence</h2>
    {_table(evidence[:200], ["id", "kind", "modality", "start_s", "end_s", "confidence", "text"])}
  </section>
</body>
</html>
"""


def _table(rows: list[dict[str, Any]], keys: list[str]) -> str:
    if not rows:
        return '<p class="muted">None.</p>'
    head = "".join(f"<th>{html.escape(key)}</th>" for key in keys)
    body_rows = []
    for row in rows:
        cells = []
        for key in keys:
            value = row.get(key)
            cells.append(f"<td>{html.escape(_format_value(value))}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return str(value)
    if value is None:
        return ""
    return str(value)


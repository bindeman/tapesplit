from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from tapesplit.costs import summarize_project_costs
from tapesplit.storage import read_jsonl


def export_review_report(project_dir: Path) -> dict:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    evidence = read_jsonl(project / "evidence.jsonl")
    claims = read_jsonl(project / "claims.jsonl")
    events = read_jsonl(project / "events.jsonl")
    summaries = read_jsonl(project / "summaries.jsonl")
    non_content = read_jsonl(project / "non_content_ranges.jsonl")
    costs = summarize_project_costs(project)
    output = project / "review.html"
    output.write_text(
        _render_html(
            tapes=tapes,
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
    tapes: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    events: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    non_content: list[dict[str, Any]],
    costs: dict[str, Any],
) -> str:
    summary_text = "\n".join(item.get("text", "") for item in summaries if item.get("text"))
    evidence_by_id = {row.get("id"): row for row in evidence if row.get("id")}
    tapes_by_id = {row.get("id"): row for row in tapes if row.get("id")}
    sorted_events = sorted(events, key=lambda row: _number_or_large(row.get("start_s")))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TapeSplit Review</title>
  <style>
    :root {{
      color-scheme: light;
      --text: #17202a;
      --muted: #627d98;
      --line: #d9e2ec;
      --panel: #ffffff;
      --soft: #f5f7fa;
      --accent: #0b6bcb;
      --warn: #8a4b08;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      color: var(--text);
      background: #f7f9fb;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 32px 24px 56px; }}
    h1, h2, h3 {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 20px; margin-bottom: 12px; }}
    h3 {{ font-size: 16px; margin-bottom: 8px; }}
    section {{ margin: 28px 0; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #edf2f7; }}
    .muted {{ color: var(--muted); }}
    .topline {{ display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .metric, .event {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .metric strong {{ display: block; font-size: 22px; margin-top: 4px; }}
    .events {{ display: grid; gap: 12px; }}
    .event-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
    .event-title {{ font-weight: 650; }}
    .event-meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }}
    .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; background: #e6f6ff; color: #074b76; font-size: 12px; }}
    .pill.warn {{ background: #fff4d6; color: var(--warn); }}
    pre {{ white-space: pre-wrap; background: var(--panel); border: 1px solid var(--line); padding: 14px; border-radius: 8px; }}
    .table-wrap {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .table-wrap table th:first-child, .table-wrap table td:first-child {{ padding-left: 12px; }}
    .table-wrap table th:last-child, .table-wrap table td:last-child {{ padding-right: 12px; }}
  </style>
</head>
<body>
  <main>
  <div class="topline">
    <div>
      <h1>TapeSplit Review</h1>
      <p class="muted">Generated from local evidence, TwelveLabs search output, and Azure extraction claims.</p>
    </div>
    <div class="muted">{len(tapes)} source video, {len(events)} event candidates, {len(claims)} claims</div>
  </div>

  <section>
    <div class="grid">
      <div class="metric"><span class="muted">Estimated Cost</span><strong>${costs.get("estimated_total_cost_usd", 0):.4f}</strong></div>
      <div class="metric"><span class="muted">Evidence Records</span><strong>{len(evidence)}</strong></div>
      <div class="metric"><span class="muted">Non-Content Ranges</span><strong>{len(non_content)}</strong></div>
      <div class="metric"><span class="muted">Unknown Cost Records</span><strong>{costs.get("unknown_cost_records", 0)}</strong></div>
    </div>
  </section>

  <section>
    <h2>Summary</h2>
    <pre>{html.escape(summary_text or "No summary generated yet.")}</pre>
  </section>

  <section>
    <h2>Event Candidates</h2>
    {_event_cards(sorted_events, evidence_by_id, tapes_by_id)}
  </section>

  <section>
    <h2>Cost Details</h2>
    {_cost_details(costs)}
  </section>

  <section>
    <h2>Claims</h2>
    <div class="table-wrap">{_table(claims, ["id", "predicate", "value", "confidence", "evidence_ids", "notes"])}</div>
  </section>

  <section>
    <h2>Non-Content Ranges</h2>
    <div class="table-wrap">{_table(non_content, ["label", "start_s", "end_s", "duration_s", "confidence"])}</div>
  </section>

  <section>
    <h2>Evidence</h2>
    <div class="table-wrap">{_table(evidence[:200], ["id", "kind", "modality", "start_s", "end_s", "confidence", "text"])}</div>
  </section>
  </main>
</body>
</html>
"""


def _event_cards(
    events: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    tapes_by_id: dict[str, dict[str, Any]],
) -> str:
    if not events:
        return '<p class="muted">None.</p>'
    cards = []
    for event in events:
        evidence_ids = [item for item in event.get("evidence_ids", []) if isinstance(item, str)]
        source_link = _source_link(event, evidence_ids, evidence_by_id, tapes_by_id)
        confidence = event.get("confidence")
        confidence_label = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else "unknown"
        review_status = event.get("review_status") or "unreviewed"
        cards.append(
            f"""
      <article class="event">
        <div class="event-head">
          <div class="event-title">{html.escape(str(event.get("title") or "Untitled event"))}</div>
          <span class="muted">{html.escape(_format_time_range(event.get("start_s"), event.get("end_s")))}</span>
        </div>
        <p>{html.escape(str(event.get("summary") or ""))}</p>
        <div class="event-meta">
          <span class="pill">confidence {html.escape(confidence_label)}</span>
          <span class="pill warn">{html.escape(str(review_status))}</span>
          <span class="pill">{len(evidence_ids)} evidence</span>
          {source_link}
        </div>
      </article>
            """.strip()
        )
    return '<div class="events">' + "\n".join(cards) + "</div>"


def _source_link(
    event: dict[str, Any],
    evidence_ids: list[str],
    evidence_by_id: dict[str, dict[str, Any]],
    tapes_by_id: dict[str, dict[str, Any]],
) -> str:
    source_video_id = None
    for evidence_id in evidence_ids:
        source_video_id = evidence_by_id.get(evidence_id, {}).get("source_video_id")
        if source_video_id:
            break
    if not source_video_id:
        return ""
    tape = tapes_by_id.get(source_video_id) or {}
    path = tape.get("path")
    if not path:
        return ""
    start_s = _number_or_none(event.get("start_s")) or 0.0
    href = Path(path).expanduser().resolve().as_uri() + f"#t={max(0, int(start_s))}"
    return f'<a class="pill" href="{html.escape(href)}">source video</a>'


def _cost_details(costs: dict[str, Any]) -> str:
    rows = []
    api = costs.get("api") or {}
    for service, detail in sorted((api.get("by_service") or {}).items()):
        rows.append(
            {
                "provider": service,
                "records": detail.get("records"),
                "units": detail.get("units"),
                "estimated_cost_usd": detail.get("estimated_cost_usd"),
                "unknown_cost_records": detail.get("unknown_cost_records"),
            }
        )
    llm = costs.get("llm") or {}
    for deployment, detail in sorted((llm.get("by_deployment") or {}).items()):
        rows.append(
            {
                "provider": deployment,
                "records": detail.get("records"),
                "units": {
                    "input_tokens": detail.get("input_tokens"),
                    "cached_input_tokens": detail.get("cached_input_tokens"),
                    "output_tokens": detail.get("output_tokens"),
                },
                "estimated_cost_usd": detail.get("estimated_cost_usd"),
                "unknown_cost_records": detail.get("unknown_cost_records"),
            }
        )
    return '<div class="table-wrap">' + _table(
        rows,
        ["provider", "records", "units", "estimated_cost_usd", "unknown_cost_records"],
    ) + "</div>"


def _table(rows: list[dict[str, Any]], keys: list[str]) -> str:
    if not rows:
        return '<p class="muted">None.</p>'
    head = "".join(f"<th>{html.escape(key)}</th>" for key in keys)
    body_rows = []
    for row in rows:
        cells = []
        for key in keys:
            value = row.get(key)
            if key in {"start_s", "end_s", "duration_s"}:
                value = _format_seconds(value)
            cells.append(f"<td>{html.escape(_format_value(value))}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return str(value)
    if value is None:
        return ""
    return str(value)


def _format_time_range(start_s: Any, end_s: Any) -> str:
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


def _number_or_large(value: Any) -> float:
    number = _number_or_none(value)
    return number if number is not None else 1_000_000_000.0


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

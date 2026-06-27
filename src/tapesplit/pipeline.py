from __future__ import annotations

from pathlib import Path
from typing import Any

from tapesplit.context_graph import build_context_graph
from tapesplit.content_classification import build_content_classifications_for_project
from tapesplit.evidence import build_evidence
from tapesplit.event_alignment import build_event_alignments
from tapesplit.event_reconciliation import build_event_reconciliations
from tapesplit.event_stitching import stitch_project_events
from tapesplit.gemini_import import import_gemini_analysis
from tapesplit.grouping import build_project_groups
from tapesplit.place_roles import build_place_roles_for_project
from tapesplit.relationships import build_relationship_candidates
from tapesplit.report import export_review_report
from tapesplit.search import DEFAULT_EMBEDDING_MODEL, build_search_index
from tapesplit.story import export_story
from tapesplit.visualization import export_visualization_data


def rebuild_project_outputs(
    project_dir: Path,
    *,
    import_gemini: bool = False,
    gemini_run_id: str | None = None,
    import_all_gemini: bool = False,
    include_duplicate_gemini_runs: bool = False,
    max_gap_seconds: float = 120.0,
    include_legacy_events: bool = False,
    alignment_context_seconds: float = 45.0,
    relationship_context_seconds: float = 8.0,
    embedding_backend: str = "local-sparse",
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    export_report: bool = True,
    export_story_output: bool = True,
    export_visualization: bool = True,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    steps = []

    if import_gemini:
        steps.append(
            {
                "step": "gemini_import",
                "result": import_gemini_analysis(
                    project,
                    run_id=gemini_run_id,
                    all_runs=import_all_gemini,
                    best_per_source=not include_duplicate_gemini_runs,
                ),
            }
        )

    steps.append({"step": "build_evidence", "result": build_evidence(project)})
    steps.append(
        {
            "step": "stitch_events",
            "result": stitch_project_events(
                project,
                max_gap_seconds=max_gap_seconds,
                prefer_gemini=not include_legacy_events,
            ),
        }
    )
    steps.append({"step": "classify_content", "result": build_content_classifications_for_project(project)})
    steps.append({"step": "build_evidence_after_classification", "result": build_evidence(project)})
    steps.append({"step": "build_groups", "result": build_project_groups(project, prefer_canonical=True)})
    steps.append({"step": "build_place_roles", "result": build_place_roles_for_project(project, prefer_canonical=True)})
    steps.append(
        {
            "step": "build_event_alignments",
            "result": build_event_alignments(project, context_seconds=alignment_context_seconds),
        }
    )
    steps.append({"step": "build_event_reconciliations", "result": build_event_reconciliations(project)})
    steps.append(
        {
            "step": "build_relationships",
            "result": build_relationship_candidates(project, context_seconds=relationship_context_seconds),
        }
    )
    steps.append({"step": "build_context_graph", "result": build_context_graph(project)})
    steps.append(
        {
            "step": "search_build",
            "result": build_search_index(
                project,
                include_groups=True,
                embedding_backend=embedding_backend,
                embedding_model=embedding_model,
            ),
        }
    )
    if export_story_output:
        steps.append({"step": "export_story", "result": export_story(project)})
    if export_report:
        steps.append({"step": "export_report", "result": export_review_report(project)})
    if export_visualization:
        steps.append({"step": "export_visualization", "result": export_visualization_data(project)})

    return {
        "project": str(project),
        "steps": steps,
    }

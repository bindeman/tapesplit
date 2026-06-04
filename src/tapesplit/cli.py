from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from tapesplit.azure_openai_adapter import check_azure_openai_config, smoke_test
from tapesplit.costs import (
    estimate_project_twelvelabs_index_cost,
    summarize_api_usage,
    summarize_llm_usage,
    summarize_project_costs,
)
from tapesplit.claims import extract_claims
from tapesplit.context_graph import build_context_graph
from tapesplit.evidence import build_evidence
from tapesplit.event_stitching import stitch_project_events
from tapesplit.evaluation import build_eval_packet, score_eval_packet
from tapesplit.geocoding import check_google_maps_config, geocode_candidate
from tapesplit.gemini_adapter import (
    analyze_project_video_chunks,
    analyze_project_video,
    check_gemini_config,
    estimate_project_video,
    smoke_test as gemini_smoke_test,
)
from tapesplit.gemini_import import import_gemini_analysis
from tapesplit.grouping import build_project_groups
from tapesplit.ingest import ingest
from tapesplit.media_metadata import extract_exif_for_project
from tapesplit.non_content import detect_non_content_for_project
from tapesplit.report import export_review_report
from tapesplit.relationships import build_relationship_candidates
from tapesplit.search import (
    DEFAULT_EMBEDDING_MODEL,
    build_search_index,
    query_search_index,
    similar_search_documents,
)
from tapesplit.transcription import (
    check_transcription_config,
    extract_project_audio,
    import_transcript,
    transcribe_project_local,
)
from tapesplit.twelvelabs_adapter import (
    check_twelvelabs_config,
    create_index,
    list_indexes,
    project_index_status,
    search_project,
    upload_project_videos,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tapesplit",
        description="Index long VHS/DVD/home-video transfers into reviewable metadata.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Inventory videos and create fixed analysis windows.",
    )
    ingest_parser.add_argument("input", type=Path, help="Video file or folder of videos.")
    ingest_parser.add_argument(
        "--out",
        type=Path,
        help="TapeSplit project output directory. Defaults to <input>.tapesplit.",
    )
    ingest_parser.add_argument(
        "--window-seconds",
        type=float,
        default=30.0,
        help="Fixed window size for search/indexing fallback. Default: 30.",
    )
    ingest_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing output project directory.",
    )

    config_parser = subparsers.add_parser(
        "doctor",
        help="Check local tool and provider configuration.",
    )
    config_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    tl_parser = subparsers.add_parser(
        "twelvelabs",
        help="Manage TwelveLabs indexes and uploads.",
    )
    tl_subparsers = tl_parser.add_subparsers(dest="twelvelabs_command", required=True)

    tl_create = tl_subparsers.add_parser("create-index", help="Create a TwelveLabs search index.")
    tl_create.add_argument("name", help="Index name.")
    tl_create.add_argument(
        "--with-pegasus",
        action="store_true",
        help="Also attach Pegasus 1.2 for indexed analysis. Marengo 3.0 is always enabled.",
    )

    tl_subparsers.add_parser("list-indexes", help="List TwelveLabs indexes.")

    tl_upload = tl_subparsers.add_parser("upload", help="Upload project videos to a TwelveLabs index.")
    tl_upload.add_argument("project", type=Path, help="TapeSplit project directory.")
    tl_upload.add_argument("--index-id", required=True, help="TwelveLabs index id.")
    tl_upload.add_argument("--wait", action="store_true", help="Wait for indexing tasks to finish.")
    tl_upload.add_argument("--limit", type=int, help="Maximum videos to upload.")
    tl_upload.add_argument(
        "--source-video-id",
        action="append",
        dest="source_video_ids",
        help="Specific source video id to upload. Can be passed multiple times.",
    )

    tl_status = tl_subparsers.add_parser("status", help="Show TwelveLabs indexed asset status.")
    tl_status.add_argument("project", type=Path, help="TapeSplit project directory.")

    tl_search = tl_subparsers.add_parser("search", help="Search an indexed project.")
    tl_search.add_argument("project", type=Path, help="TapeSplit project directory.")
    tl_search.add_argument("query", help="Natural-language search query.")
    tl_search.add_argument("--limit", type=int, default=10, help="Max results. Default: 10.")
    tl_search.add_argument(
        "--options",
        default="visual,audio",
        help="Comma-separated TwelveLabs search options. Default: visual,audio.",
    )

    azure_parser = subparsers.add_parser(
        "azure",
        help="Run Azure OpenAI checks and utilities.",
    )
    azure_subparsers = azure_parser.add_subparsers(dest="azure_command", required=True)
    azure_smoke = azure_subparsers.add_parser("smoke", help="Make a tiny Azure OpenAI test call.")
    azure_smoke.add_argument(
        "--deployment",
        default="fast",
        help="Deployment alias or name. Aliases: fast, reasoning, full, chat, codex.",
    )
    azure_smoke.add_argument(
        "--project",
        type=Path,
        help="Optional TapeSplit project directory where token usage should be logged.",
    )

    gemini_parser = subparsers.add_parser(
        "gemini",
        help="Run Vertex Gemini checks and video analysis.",
    )
    gemini_subparsers = gemini_parser.add_subparsers(dest="gemini_command", required=True)
    gemini_smoke = gemini_subparsers.add_parser("smoke", help="Make a tiny Vertex Gemini test call.")
    gemini_smoke.add_argument(
        "--project",
        type=Path,
        help="Optional TapeSplit project directory where usage should be logged.",
    )
    gemini_estimate = gemini_subparsers.add_parser(
        "estimate-video",
        help="Estimate Gemini video analysis tokens and cost before calling the model.",
    )
    gemini_estimate.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_estimate.add_argument("--source-video-id", help="Source video id. Defaults to first video.")
    gemini_estimate.add_argument("--fps", type=float, help="Video sampling FPS. Defaults to GEMINI_DEFAULT_FPS.")
    gemini_estimate.add_argument(
        "--media-resolution",
        choices=["low", "medium", "high"],
        help="Gemini media resolution. Defaults to GEMINI_MEDIA_RESOLUTION.",
    )
    gemini_estimate.add_argument(
        "--output-tokens",
        type=int,
        default=6000,
        help="Estimated output tokens. Default: 6000.",
    )
    gemini_estimate.add_argument(
        "--chunk-seconds",
        type=float,
        help="Estimate a chunked run with this chunk duration instead of one whole-video call.",
    )
    gemini_estimate.add_argument(
        "--chunk-overlap-seconds",
        type=float,
        default=0.0,
        help="Chunk overlap for chunked estimates. Default: 0.",
    )
    gemini_analyze = gemini_subparsers.add_parser(
        "analyze-video",
        help="Analyze a project video with Vertex Gemini and write gemini_analyses.jsonl.",
    )
    gemini_analyze.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_analyze.add_argument("--source-video-id", help="Source video id. Defaults to first video.")
    gemini_analyze.add_argument("--fps", type=float, help="Video sampling FPS. Defaults to GEMINI_DEFAULT_FPS.")
    gemini_analyze.add_argument(
        "--media-resolution",
        choices=["low", "medium", "high"],
        help="Gemini media resolution. Defaults to GEMINI_MEDIA_RESOLUTION.",
    )
    gemini_analyze.add_argument("--max-output-tokens", type=int, default=12000)
    gemini_analyze.add_argument("--force-upload", action="store_true")
    gemini_analyze_chunks = gemini_subparsers.add_parser(
        "analyze-video-chunks",
        help="Analyze a project video with Gemini in source-offset-preserving chunks.",
    )
    gemini_analyze_chunks.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_analyze_chunks.add_argument("--source-video-id", help="Source video id. Defaults to first video.")
    gemini_analyze_chunks.add_argument("--fps", type=float, help="Video sampling FPS. Defaults to GEMINI_DEFAULT_FPS.")
    gemini_analyze_chunks.add_argument(
        "--media-resolution",
        choices=["low", "medium", "high"],
        help="Gemini media resolution. Defaults to GEMINI_MEDIA_RESOLUTION.",
    )
    gemini_analyze_chunks.add_argument("--max-output-tokens", type=int, default=8000)
    gemini_analyze_chunks.add_argument(
        "--chunk-seconds",
        type=float,
        default=900.0,
        help="Chunk duration in seconds. Default: 900.",
    )
    gemini_analyze_chunks.add_argument(
        "--chunk-overlap-seconds",
        type=float,
        default=15.0,
        help="Context overlap between chunks in seconds. Default: 15.",
    )
    gemini_analyze_chunks.add_argument(
        "--limit-chunks",
        type=int,
        help="Analyze only the first N chunks for a bounded test run.",
    )
    gemini_analyze_chunks.add_argument(
        "--run-id",
        help="Append results to an existing Gemini analysis_run_id instead of creating a new run.",
    )
    gemini_analyze_chunks.add_argument(
        "--start-chunk",
        type=int,
        help="Start at this 1-based chunk index. Useful for resuming after a failed chunk.",
    )
    gemini_analyze_chunks.add_argument("--force-clips", action="store_true")
    gemini_analyze_chunks.add_argument("--force-upload", action="store_true")
    gemini_import = gemini_subparsers.add_parser(
        "import-analysis",
        help="Normalize the latest Gemini analysis into reviewable event/evidence JSONL.",
    )
    gemini_import.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_import.add_argument("--run-id", help="Import a specific Gemini analysis_run_id.")
    gemini_import.add_argument("--all", action="store_true", help="Import all Gemini analysis runs.")

    costs_parser = subparsers.add_parser(
        "costs",
        help="Summarize tracked provider usage and estimated costs.",
    )
    costs_subparsers = costs_parser.add_subparsers(dest="costs_command", required=True)
    costs_llm = costs_subparsers.add_parser("llm", help="Summarize LLM token/cost ledger.")
    costs_llm.add_argument("project", type=Path, help="TapeSplit project directory.")
    costs_api = costs_subparsers.add_parser("api", help="Summarize non-LLM provider usage/cost ledger.")
    costs_api.add_argument("project", type=Path, help="TapeSplit project directory.")
    costs_project = costs_subparsers.add_parser("project", help="Summarize all tracked project costs.")
    costs_project.add_argument("project", type=Path, help="TapeSplit project directory.")
    costs_estimate = costs_subparsers.add_parser(
        "estimate-twelvelabs-index",
        help="Estimate TwelveLabs indexing cost from project video durations before upload.",
    )
    costs_estimate.add_argument("project", type=Path, help="TapeSplit project directory.")

    detect_parser = subparsers.add_parser(
        "detect-non-content",
        help="Detect blank/blue/static/no-signal ranges in a project.",
    )
    detect_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    detect_parser.add_argument(
        "--sample-interval",
        type=float,
        default=2.0,
        help="Seconds between sampled frames. Default: 2.",
    )
    detect_parser.add_argument(
        "--min-range",
        type=float,
        default=4.0,
        help="Minimum merged non-content range duration. Default: 4.",
    )

    metadata_parser = subparsers.add_parser(
        "metadata",
        help="Extract local media metadata.",
    )
    metadata_subparsers = metadata_parser.add_subparsers(dest="metadata_command", required=True)
    metadata_exif = metadata_subparsers.add_parser("exif", help="Load ExifTool metadata for project videos.")
    metadata_exif.add_argument("project", type=Path, help="TapeSplit project directory.")

    transcribe_parser = subparsers.add_parser(
        "transcribe",
        help="Extract/import local transcript segments.",
    )
    transcribe_subparsers = transcribe_parser.add_subparsers(dest="transcribe_command", required=True)
    transcribe_audio = transcribe_subparsers.add_parser("extract-audio", help="Extract 16 kHz mono WAV audio for local ASR.")
    transcribe_audio.add_argument("project", type=Path, help="TapeSplit project directory.")
    transcribe_audio.add_argument("--source-video-id", help="Source video id. Defaults to all videos.")
    transcribe_audio.add_argument("--force", action="store_true", help="Overwrite existing audio files.")

    transcribe_local = transcribe_subparsers.add_parser("local", help="Run a local Whisper-compatible transcription CLI.")
    transcribe_local.add_argument("project", type=Path, help="TapeSplit project directory.")
    transcribe_local.add_argument("--source-video-id", help="Source video id. Defaults to all videos.")
    transcribe_local.add_argument(
        "--engine",
        choices=["auto", "whisper", "whisper-cpp"],
        default="auto",
        help="Local transcription engine. Default: auto.",
    )
    transcribe_local.add_argument(
        "--model",
        default="large-v3-turbo",
        help="openai-whisper model name. Default: large-v3-turbo.",
    )
    transcribe_local.add_argument(
        "--model-path",
        type=Path,
        help="whisper.cpp ggml model path. Can also use WHISPER_CPP_MODEL.",
    )
    transcribe_local.add_argument(
        "--language",
        help="Optional ASR language hint, e.g. ru or en. Omit for auto-detect.",
    )
    transcribe_local.add_argument("--force", action="store_true", help="Replace existing transcript segments for the selected source.")

    transcribe_import = transcribe_subparsers.add_parser("import", help="Import an existing JSON/SRT/VTT transcript.")
    transcribe_import.add_argument("project", type=Path, help="TapeSplit project directory.")
    transcribe_import.add_argument("transcript", type=Path, help="Transcript file to import.")
    transcribe_import.add_argument("--source-video-id", required=True, help="Source video id for the transcript.")
    transcribe_import.add_argument(
        "--format",
        choices=["auto", "json", "srt", "vtt"],
        default="auto",
        help="Transcript format. Default: auto.",
    )
    transcribe_import.add_argument("--language", help="Language code for imported segments if missing.")
    transcribe_import.add_argument(
        "--offset-seconds",
        type=float,
        default=0.0,
        help="Add this source-time offset to imported transcript times. Default: 0.",
    )
    transcribe_import.add_argument("--force", action="store_true", help="Replace existing transcript segments for this source.")

    geocode_parser = subparsers.add_parser(
        "geocode",
        help="Build or resolve geocoding candidates.",
    )
    geocode_subparsers = geocode_parser.add_subparsers(dest="geocode_command", required=True)
    geocode_candidate_parser = geocode_subparsers.add_parser(
        "candidate",
        help="Build or resolve a place candidate.",
    )
    geocode_candidate_parser.add_argument("name", help="Observed place name.")
    geocode_candidate_parser.add_argument("--city", help="City hint.")
    geocode_candidate_parser.add_argument("--region", help="State/region hint.")
    geocode_candidate_parser.add_argument("--country", help="Country hint.")
    geocode_candidate_parser.add_argument("--project", type=Path, help="Optional project for cost logging.")
    geocode_candidate_parser.add_argument(
        "--allow-api",
        action="store_true",
        help="Actually call Google Maps if config is enabled and budget is positive.",
    )
    geocode_candidate_parser.add_argument("--max-results", type=int, default=5)

    build_evidence_parser = subparsers.add_parser(
        "build-evidence",
        help="Normalize project artifacts into evidence.jsonl.",
    )
    build_evidence_parser.add_argument("project", type=Path, help="TapeSplit project directory.")

    extract_claims_parser = subparsers.add_parser(
        "extract-claims",
        help="Use Azure OpenAI to extract grounded claims/events from evidence.",
    )
    extract_claims_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    extract_claims_parser.add_argument(
        "--deployment",
        default="fast",
        help="Azure deployment alias/name. Default: fast.",
    )

    stitch_events_parser = subparsers.add_parser(
        "stitch-events",
        help="Build canonical events from raw event candidates.",
    )
    stitch_events_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    stitch_events_parser.add_argument(
        "--max-gap-seconds",
        type=float,
        default=120.0,
        help="Maximum gap for merging related boundary-split events. Default: 120.",
    )
    stitch_events_parser.add_argument(
        "--include-legacy-events",
        action="store_true",
        help="Include non-Gemini events even when Gemini events exist.",
    )

    build_groups_parser = subparsers.add_parser(
        "build-groups",
        help="Build local people/place/date/language/event/album group projections.",
    )
    build_groups_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    build_groups_parser.add_argument(
        "--use-event-candidates",
        action="store_true",
        help="Use raw event candidates instead of canonical_events.jsonl.",
    )

    build_relationships_parser = subparsers.add_parser(
        "build-relationships",
        help="Build local transcript-derived relationship candidates and review tasks.",
    )
    build_relationships_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    build_relationships_parser.add_argument(
        "--context-seconds",
        type=float,
        default=8.0,
        help="Transcript context window around kinship terms. Default: 8.",
    )

    build_context_graph_parser = subparsers.add_parser(
        "build-context-graph",
        help="Build local context graph edges and edge metrics.",
    )
    build_context_graph_parser.add_argument("project", type=Path, help="TapeSplit project directory.")

    search_parser = subparsers.add_parser(
        "search",
        help="Build and query the local SQLite text/semantic search index.",
    )
    search_subparsers = search_parser.add_subparsers(dest="search_command", required=True)
    search_build = search_subparsers.add_parser("build", help="Build a local search.sqlite index.")
    search_build.add_argument("project", type=Path, help="TapeSplit project directory.")
    search_build.add_argument(
        "--no-groups",
        action="store_true",
        help="Exclude album/person/place/date/language group projections.",
    )
    search_build.add_argument(
        "--embedding-backend",
        choices=["local-sparse", "sentence-transformers", "auto"],
        default="local-sparse",
        help="Semantic embedding backend. Default: local-sparse.",
    )
    search_build.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="SentenceTransformers model when --embedding-backend sentence-transformers/auto is used.",
    )
    search_query = search_subparsers.add_parser("query", help="Search local transcript/evidence/event/group text.")
    search_query.add_argument("project", type=Path, help="TapeSplit project directory.")
    search_query.add_argument("query", help="Natural-language search query.")
    search_query.add_argument("--limit", type=int, default=10, help="Max results. Default: 10.")
    search_similar = search_subparsers.add_parser("similar", help="Find indexed records similar to an existing record.")
    search_similar.add_argument("project", type=Path, help="TapeSplit project directory.")
    search_similar.add_argument("source_id", help="Source id such as canonical_event_000001 or event:canonical_event_000001.")
    search_similar.add_argument(
        "--record-type",
        choices=[
            "album",
            "context_edge",
            "date_group",
            "edge_metric",
            "event",
            "event_group",
            "evidence",
            "language_group",
            "people_group",
            "place_group",
            "relationship_candidate",
            "relationship_review_task",
            "transcript",
        ],
        help="Disambiguate source ids shared across record types.",
    )
    search_similar.add_argument("--limit", type=int, default=10, help="Max results. Default: 10.")

    eval_parser = subparsers.add_parser(
        "eval",
        help="Build and score family-review evaluation packets.",
    )
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_build = eval_subparsers.add_parser(
        "build",
        help="Create a reviewer packet with JSONL, CSV, and SQLite artifacts.",
    )
    eval_build.add_argument("project", type=Path, help="TapeSplit project directory.")
    eval_build.add_argument(
        "--out",
        type=Path,
        help="Output directory. Defaults to <project>/eval_packet.",
    )
    eval_build.add_argument(
        "--max-items",
        type=int,
        default=200,
        help="Maximum review items to include. Use 0 for all. Default: 200.",
    )
    eval_build.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing eval packet directory.",
    )
    eval_score = eval_subparsers.add_parser(
        "score",
        help="Score completed annotations and produce follow-up queues.",
    )
    eval_score.add_argument("project", type=Path, help="TapeSplit project directory.")
    eval_score.add_argument(
        "--eval-dir",
        type=Path,
        help="Eval packet directory. Defaults to <project>/eval_packet.",
    )
    eval_score.add_argument(
        "--annotations",
        type=Path,
        help="Completed annotations JSONL or CSV. Defaults to annotations.jsonl or annotations.csv in the eval packet.",
    )

    report_parser = subparsers.add_parser(
        "export-report",
        help="Export a static review.html report.",
    )
    report_parser.add_argument("project", type=Path, help="TapeSplit project directory.")

    return parser


def _doctor(as_json: bool) -> int:
    status = check_twelvelabs_config()
    status.update(check_azure_openai_config())
    status.update(check_google_maps_config())
    status.update(check_gemini_config())
    status.update(check_transcription_config())
    status["ffprobe"] = shutil.which("ffprobe") is not None
    status["ffmpeg"] = shutil.which("ffmpeg") is not None
    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print("TapeSplit doctor")
        print(f"  ffmpeg: {'installed' if status['ffmpeg'] else 'missing'}")
        print(f"  ffprobe: {'installed' if status['ffprobe'] else 'missing'}")
        print(f"  TwelveLabs key: {'configured' if status['twelvelabs_api_key'] else 'missing'}")
        print(f"  TwelveLabs SDK: {'installed' if status['twelvelabs_sdk'] else 'not installed'}")
        print(f"  Azure OpenAI endpoint: {'configured' if status['azure_openai_endpoint'] else 'missing'}")
        print(f"  Azure OpenAI key: {'configured' if status['azure_openai_api_key'] else 'missing'}")
        print(f"  Azure OpenAI API version: {status['azure_openai_api_version'] or 'missing'}")
        print(f"  Azure OpenAI region: {status['azure_openai_region'] or 'missing'}")
        print("  Azure OpenAI deployments:")
        for name, deployment in status["azure_openai_deployments"].items():
            print(f"    {name}: {deployment or 'missing'}")
        print(f"  Google Maps key: {'configured' if status['google_maps_api_key'] else 'missing'}")
        print(f"  Google Maps enabled: {status['google_maps_enabled']}")
        print(f"  Google Maps daily budget USD: {status['google_maps_daily_budget_usd']}")
        print(f"  Gemini Vertex configured: {status['gemini_configured']}")
        print(f"  Gemini ADC: {'configured' if status['gemini_adc'] else 'missing'}")
        print(f"  Gemini project: {status['gemini_project'] or 'missing'}")
        print(f"  Gemini location: {status['gemini_location']}")
        print(f"  Gemini model: {status['gemini_model']}")
        print(f"  Gemini media resolution: {status['gemini_media_resolution']}")
        print(f"  Gemini GCS bucket: {'configured' if status['gemini_gcs_bucket'] else 'missing'}")
        print(f"  openai-whisper CLI: {'installed' if status['whisper_cli'] else 'missing'}")
        print(f"  whisper.cpp whisper-cli: {'installed' if status['whisper_cpp_cli'] else 'missing'}")
        print(f"  whisper.cpp main: {'installed' if status['whisper_cpp_main'] else 'missing'}")
        print(f"  WHISPER_CPP_MODEL: {'configured' if status['whisper_cpp_model'] else 'missing'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "ingest":
            result = ingest(
                input_path=args.input,
                out_path=args.out,
                window_seconds=args.window_seconds,
                force=args.force,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "doctor":
            return _doctor(as_json=args.json)
        if args.command == "twelvelabs":
            if args.twelvelabs_command == "create-index":
                print(json.dumps(create_index(args.name, include_pegasus=args.with_pegasus), indent=2))
                return 0
            if args.twelvelabs_command == "list-indexes":
                print(json.dumps(list_indexes(), indent=2))
                return 0
            if args.twelvelabs_command == "upload":
                result = upload_project_videos(
                    project_dir=args.project,
                    index_id=args.index_id,
                    wait=args.wait,
                    limit=args.limit,
                    source_video_ids=args.source_video_ids,
                )
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            if args.twelvelabs_command == "status":
                print(json.dumps(project_index_status(args.project), indent=2, sort_keys=True))
                return 0
            if args.twelvelabs_command == "search":
                print(
                    json.dumps(
                        search_project(
                            args.project,
                            args.query,
                            page_limit=args.limit,
                            search_options=[
                                option.strip()
                                for option in args.options.split(",")
                                if option.strip()
                            ],
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "azure":
            if args.azure_command == "smoke":
                print(
                    json.dumps(
                        smoke_test(args.deployment, project_dir=args.project),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "gemini":
            if args.gemini_command == "smoke":
                print(
                    json.dumps(
                        gemini_smoke_test(project_dir=args.project),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "estimate-video":
                print(
                    json.dumps(
                        estimate_project_video(
                            args.project,
                            source_video_id=args.source_video_id,
                            fps=args.fps,
                            media_resolution=args.media_resolution,
                            output_tokens=args.output_tokens,
                            chunk_seconds=args.chunk_seconds,
                            chunk_overlap_seconds=args.chunk_overlap_seconds,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "analyze-video":
                print(
                    json.dumps(
                        analyze_project_video(
                            args.project,
                            source_video_id=args.source_video_id,
                            fps=args.fps,
                            media_resolution=args.media_resolution,
                            max_output_tokens=args.max_output_tokens,
                            force_upload=args.force_upload,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "analyze-video-chunks":
                print(
                    json.dumps(
                        analyze_project_video_chunks(
                            args.project,
                            source_video_id=args.source_video_id,
                            fps=args.fps,
                            media_resolution=args.media_resolution,
                            max_output_tokens=args.max_output_tokens,
                            chunk_seconds=args.chunk_seconds,
                            chunk_overlap_seconds=args.chunk_overlap_seconds,
                            run_id=args.run_id,
                            start_chunk=args.start_chunk,
                            limit_chunks=args.limit_chunks,
                            force_clips=args.force_clips,
                            force_upload=args.force_upload,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "import-analysis":
                print(
                    json.dumps(
                        import_gemini_analysis(args.project, run_id=args.run_id, all_runs=args.all),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "costs":
            if args.costs_command == "llm":
                print(json.dumps(summarize_llm_usage(args.project), indent=2, sort_keys=True))
                return 0
            if args.costs_command == "api":
                print(json.dumps(summarize_api_usage(args.project), indent=2, sort_keys=True))
                return 0
            if args.costs_command == "project":
                print(json.dumps(summarize_project_costs(args.project), indent=2, sort_keys=True))
                return 0
            if args.costs_command == "estimate-twelvelabs-index":
                print(
                    json.dumps(
                        estimate_project_twelvelabs_index_cost(args.project),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "detect-non-content":
            print(
                json.dumps(
                    detect_non_content_for_project(
                        args.project,
                        sample_interval_s=args.sample_interval,
                        min_range_s=args.min_range,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "metadata":
            if args.metadata_command == "exif":
                print(json.dumps(extract_exif_for_project(args.project), indent=2, sort_keys=True))
                return 0
        if args.command == "transcribe":
            if args.transcribe_command == "extract-audio":
                print(
                    json.dumps(
                        extract_project_audio(
                            args.project,
                            source_video_id=args.source_video_id,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.transcribe_command == "local":
                print(
                    json.dumps(
                        transcribe_project_local(
                            args.project,
                            source_video_id=args.source_video_id,
                            engine=args.engine,
                            model=args.model,
                            model_path=args.model_path,
                            language=args.language,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.transcribe_command == "import":
                print(
                    json.dumps(
                        import_transcript(
                            args.project,
                            args.transcript,
                            source_video_id=args.source_video_id,
                            transcript_format=args.format,
                            language=args.language,
                            offset_seconds=args.offset_seconds,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "geocode":
            if args.geocode_command == "candidate":
                print(
                    json.dumps(
                        geocode_candidate(
                            name=args.name,
                            city=args.city,
                            region=args.region,
                            country=args.country,
                            project_dir=args.project,
                            allow_api=args.allow_api,
                            max_results=args.max_results,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "build-evidence":
            print(json.dumps(build_evidence(args.project), indent=2, sort_keys=True))
            return 0
        if args.command == "extract-claims":
            print(
                json.dumps(
                    extract_claims(args.project, deployment_alias=args.deployment),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "stitch-events":
            print(
                json.dumps(
                    stitch_project_events(
                        args.project,
                        max_gap_seconds=args.max_gap_seconds,
                        prefer_gemini=not args.include_legacy_events,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "build-groups":
            print(
                json.dumps(
                    build_project_groups(
                        args.project,
                        prefer_canonical=not args.use_event_candidates,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "build-relationships":
            print(
                json.dumps(
                    build_relationship_candidates(
                        args.project,
                        context_seconds=args.context_seconds,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "build-context-graph":
            print(json.dumps(build_context_graph(args.project), indent=2, sort_keys=True))
            return 0
        if args.command == "search":
            if args.search_command == "build":
                print(
                    json.dumps(
                        build_search_index(
                            args.project,
                            include_groups=not args.no_groups,
                            embedding_backend=args.embedding_backend,
                            embedding_model=args.embedding_model,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.search_command == "query":
                print(
                    json.dumps(
                        query_search_index(args.project, args.query, limit=args.limit),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.search_command == "similar":
                print(
                    json.dumps(
                        similar_search_documents(
                            args.project,
                            args.source_id,
                            record_type=args.record_type,
                            limit=args.limit,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "eval":
            if args.eval_command == "build":
                print(
                    json.dumps(
                        build_eval_packet(
                            args.project,
                            out_dir=args.out,
                            max_items=args.max_items,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.eval_command == "score":
                print(
                    json.dumps(
                        score_eval_packet(
                            args.project,
                            eval_dir=args.eval_dir,
                            annotations_path=args.annotations,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.command == "export-report":
            print(json.dumps(export_review_report(args.project), indent=2, sort_keys=True))
            return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

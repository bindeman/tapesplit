from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from tapesplit.auto import (
    AutoOptions,
    DEFAULT_MAX_CLOUD_USD,
    PROFILES,
    project_status,
    run_auto,
    stage_names,
)
from tapesplit.azure_openai_adapter import check_azure_openai_config, smoke_test
from tapesplit.costs import (
    estimate_project_twelvelabs_index_cost,
    summarize_api_usage,
    summarize_llm_usage,
    summarize_project_costs,
)
from tapesplit.claims import extract_claims
from tapesplit.context_graph import build_context_graph
from tapesplit.content_classification import build_content_classifications_for_project
from tapesplit.evidence import build_evidence
from tapesplit.event_stitching import stitch_project_events
from tapesplit.evaluation import build_eval_packet, score_eval_packet
from tapesplit.event_alignment import build_event_alignments
from tapesplit.event_reconciliation import build_event_reconciliations
from tapesplit.face_clustering import (
    DEFAULT_FACE_EMBEDDING_BACKEND,
    DEFAULT_FACE_CLUSTER_DISTANCE,
    check_face_embedding_config,
    cluster_faces_for_project,
)
from tapesplit.faces import (
    DEFAULT_FACE_DETECTION_BACKEND,
    DEFAULT_FACE_MIN_SIZE,
    check_face_detection_config,
    detect_face_thumbnails_for_project,
)
from tapesplit.geocoding import check_google_maps_config, geocode_candidate
from tapesplit.gemini_adapter import (
    analyze_project_video_chunks,
    analyze_project_video,
    analyze_project_videos,
    check_gemini_config,
    estimate_project_video,
    estimate_project_videos,
    prepare_project_video_proxies,
    smoke_test as gemini_smoke_test,
)
from tapesplit.gemini_compare import compare_gemini_analysis_modes, summarize_gemini_analyses
from tapesplit.gemini_import import import_gemini_analysis
from tapesplit.grouping import build_project_groups
from tapesplit.heuristic_events import build_heuristic_events
from tapesplit.ingest import ingest
from tapesplit.media_metadata import extract_exif_for_project
from tapesplit.non_content import detect_non_content_for_project
from tapesplit.place_roles import build_place_roles_for_project
from tapesplit.pipeline import rebuild_project_outputs
from tapesplit.report import export_review_report
from tapesplit.relationships import build_relationship_candidates
from tapesplit.review_actions import (
    apply_review_actions,
    apply_review_suggestions,
    list_review_corrections,
    reapply_review_corrections,
)
from tapesplit.scenes import (
    DEFAULT_MIN_SCENE_SECONDS,
    DEFAULT_SCENE_THRESHOLD,
    detect_scenes_for_project,
)
from tapesplit.search import (
    DEFAULT_EMBEDDING_MODEL,
    build_search_index,
    query_search_index,
    similar_search_documents,
)
from tapesplit.speaker_identity import (
    DEFAULT_MAX_CANDIDATES_PER_SPEAKER,
    DEFAULT_MIN_SPEAKER_IDENTITY_CONFIDENCE,
    build_speaker_identity_candidates,
)
from tapesplit.speakers import (
    DEFAULT_SPEAKER_DIARIZATION_BACKEND,
    DEFAULT_SPEAKER_DIARIZATION_MODEL,
    check_speaker_diarization_config,
    diarize_project_speakers,
    import_speaker_segments,
)
from tapesplit.story import export_story
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
from tapesplit.visual_assets import (
    DEFAULT_KEYFRAME_WIDTH,
    DEFAULT_THUMBNAIL_WIDTH,
    extract_visual_assets_for_project,
)
from tapesplit.visual_captions import (
    DEFAULT_VISUAL_CAPTION_BACKEND,
    DEFAULT_VISUAL_CAPTION_MODEL,
    caption_visual_assets_for_project,
    check_visual_caption_config,
)
from tapesplit.visual_embeddings import (
    DEFAULT_VISUAL_EMBEDDING_BACKEND,
    DEFAULT_VISUAL_EMBEDDING_MODEL,
    build_visual_similarity_for_project,
    check_visual_embedding_config,
    embed_visual_assets_for_project,
)
from tapesplit.visual_text import (
    DEFAULT_TEXT_MIN_CONFIDENCE,
    DEFAULT_TEXT_RECOGNITION_BACKEND,
    check_visual_text_config,
    detect_text_for_project,
)
from tapesplit.visualization import export_visualization_data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tapesplit",
        description="Index long VHS/DVD/home-video transfers into reviewable metadata.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auto_parser = subparsers.add_parser(
        "auto",
        help="Run the full pipeline: raw videos in, reviewable archive out.",
    )
    auto_parser.add_argument(
        "input",
        type=Path,
        help="Video file, folder of videos, or existing .tapesplit project.",
    )
    auto_parser.add_argument(
        "--out",
        type=Path,
        help="Project output directory. Defaults to <input>.tapesplit.",
    )
    auto_parser.add_argument(
        "--profile",
        choices=PROFILES,
        default="auto",
        help="auto: use everything available; local: no cloud calls; minimal: deterministic stages only.",
    )
    auto_parser.add_argument(
        "--max-cloud-usd",
        type=float,
        default=DEFAULT_MAX_CLOUD_USD,
        help=f"Skip cloud video analysis if the estimate exceeds this. Default: {DEFAULT_MAX_CLOUD_USD}. Pass -1 to disable the gate.",
    )
    auto_parser.add_argument("--language", help="Transcription language hint, e.g. ru or en.")
    auto_parser.add_argument(
        "--window-seconds",
        type=float,
        default=30.0,
        help="Ingest window size for search/indexing fallback. Default: 30.",
    )
    auto_parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="STAGE",
        help=f"Skip a stage (repeatable). Stages: {', '.join(stage_names())}.",
    )
    auto_parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="STAGE",
        help="Run only the named stages (repeatable).",
    )
    auto_parser.add_argument("--force", action="store_true", help="Re-run stages even if already completed.")
    auto_parser.add_argument(
        "--force-from",
        metavar="STAGE",
        help="Re-run the named stage and everything after it.",
    )
    auto_parser.add_argument(
        "--suggestions-tier",
        choices=["primary", "backlog", "all"],
        default="primary",
        help="Auto-accept review suggestions from this tier after the build. Default: primary.",
    )
    auto_parser.add_argument(
        "--no-suggestions",
        action="store_true",
        help="Do not auto-accept any review suggestions.",
    )
    auto_parser.add_argument(
        "--suggestions-min-confidence",
        type=float,
        help="Only auto-accept suggestions at or above this confidence.",
    )
    auto_parser.add_argument(
        "--search-embedding-backend",
        choices=["local-sparse", "sentence-transformers", "auto"],
        default="local-sparse",
        help="Embedding backend for the search index. Default: local-sparse.",
    )
    auto_parser.add_argument("--plan", action="store_true", help="Print the stage plan and exit without running.")
    auto_parser.add_argument("--json", action="store_true", help="Print the machine-readable result JSON.")

    status_parser = subparsers.add_parser(
        "status",
        help="Show pipeline stage state and archive metrics for a project.",
    )
    status_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    ui_parser = subparsers.add_parser(
        "ui",
        help="Launch the local review UI against a project.",
    )
    ui_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    ui_parser.add_argument("--port", type=int, help="Preferred dev server port (default 5173).")

    synth_parser = subparsers.add_parser(
        "synthesize-events",
        help="Synthesize low-confidence local events for tapes without cloud analysis.",
    )
    synth_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    synth_parser.add_argument(
        "--include-covered",
        action="store_true",
        help="Also synthesize for tapes already covered by analyzed events.",
    )

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
    gemini_estimate.add_argument("--all", action="store_true", help="Estimate all project videos.")
    gemini_estimate.add_argument(
        "--uploaded-to-twelvelabs-only",
        action="store_true",
        help="With --all, only include videos with TwelveLabs upload records.",
    )
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
    gemini_prepare = gemini_subparsers.add_parser(
        "prepare-video",
        help="Create Gemini-ready whole-video analysis proxies under the upload size limit.",
    )
    gemini_prepare.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_prepare.add_argument("--source-video-id", help="Source video id. Defaults to all videos.")
    gemini_prepare.add_argument(
        "--max-upload-gb",
        type=float,
        default=1.45,
        help="Maximum proxy/upload size in decimal GB. Default: 1.45.",
    )
    gemini_prepare.add_argument("--height", type=int, default=480, help="Proxy video height. Default: 480.")
    gemini_prepare.add_argument("--proxy-fps", type=float, default=12.0, help="Proxy video FPS. Default: 12.")
    gemini_prepare.add_argument("--force", action="store_true", help="Recreate existing proxies.")
    gemini_analyze = gemini_subparsers.add_parser(
        "analyze-video",
        help="Analyze a project video with Vertex Gemini and write gemini_analyses.jsonl.",
    )
    gemini_analyze.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_analyze.add_argument("--source-video-id", help="Source video id. Defaults to first video.")
    gemini_analyze.add_argument("--all", action="store_true", help="Analyze all project videos.")
    gemini_analyze.add_argument(
        "--uploaded-to-twelvelabs-only",
        action="store_true",
        help="With --all, only analyze videos with TwelveLabs upload records.",
    )
    gemini_analyze.add_argument("--fps", type=float, help="Video sampling FPS. Defaults to GEMINI_DEFAULT_FPS.")
    gemini_analyze.add_argument(
        "--media-resolution",
        choices=["low", "medium", "high"],
        help="Gemini media resolution. Defaults to GEMINI_MEDIA_RESOLUTION.",
    )
    gemini_analyze.add_argument("--max-output-tokens", type=int, default=8000)
    gemini_analyze.add_argument("--force-upload", action="store_true")
    gemini_analyze.add_argument(
        "--no-proxy",
        action="store_true",
        help="Upload the original video directly instead of creating a whole-tape proxy.",
    )
    gemini_analyze.add_argument("--force-proxy", action="store_true", help="Recreate the whole-tape proxy before upload.")
    gemini_analyze.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue a batch run after a video fails and log errors.",
    )
    gemini_analyze.add_argument(
        "--max-upload-gb",
        type=float,
        default=1.45,
        help="Maximum proxy/upload size in decimal GB. Default: 1.45.",
    )
    gemini_analyze.add_argument("--proxy-height", type=int, default=480, help="Proxy video height. Default: 480.")
    gemini_analyze.add_argument("--proxy-fps", type=float, default=12.0, help="Proxy video FPS. Default: 12.")
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
    gemini_import.add_argument(
        "--all",
        action="store_true",
        help="Import the best available analysis per source video across Gemini runs.",
    )
    gemini_import.add_argument(
        "--include-duplicate-runs",
        action="store_true",
        help="With --all, import every Gemini analysis row, including older duplicate runs.",
    )
    gemini_summary = gemini_subparsers.add_parser(
        "summarize-analyses",
        help="Summarize Gemini analysis coverage, entities, and follow-up signals.",
    )
    gemini_summary.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_summary.add_argument("--source-video-id", help="Limit summary to one source video id.")
    gemini_compare = gemini_subparsers.add_parser(
        "compare-analyses",
        help="Compare chunked Gemini analysis against whole-tape Gemini analysis.",
    )
    gemini_compare.add_argument("project", type=Path, help="TapeSplit project directory.")
    gemini_compare.add_argument("--source-video-id", help="Limit comparison to one source video id.")

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

    scenes_parser = subparsers.add_parser(
        "detect-scenes",
        help="Detect local visual scene intervals and write scenes.jsonl.",
    )
    scenes_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    scenes_parser.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    scenes_parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_SCENE_THRESHOLD,
        help=f"ffmpeg scene-change threshold. Default: {DEFAULT_SCENE_THRESHOLD}.",
    )
    scenes_parser.add_argument(
        "--min-scene-seconds",
        type=float,
        default=DEFAULT_MIN_SCENE_SECONDS,
        help=f"Merge content scenes shorter than this duration. Default: {DEFAULT_MIN_SCENE_SECONDS}.",
    )

    metadata_parser = subparsers.add_parser(
        "metadata",
        help="Extract local media metadata.",
    )
    metadata_subparsers = metadata_parser.add_subparsers(dest="metadata_command", required=True)
    metadata_exif = metadata_subparsers.add_parser("exif", help="Load ExifTool metadata for project videos.")
    metadata_exif.add_argument("project", type=Path, help="TapeSplit project directory.")

    visuals_parser = subparsers.add_parser(
        "extract-visuals",
        help="Extract scene/event keyframes and thumbnails.",
    )
    visuals_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    visuals_parser.add_argument(
        "--subjects",
        default="scenes,events",
        help="Comma-separated subjects: scenes,events. Default: scenes,events.",
    )
    visuals_parser.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    visuals_parser.add_argument(
        "--keyframe-width",
        type=int,
        default=DEFAULT_KEYFRAME_WIDTH,
        help=f"Keyframe image width. Default: {DEFAULT_KEYFRAME_WIDTH}.",
    )
    visuals_parser.add_argument(
        "--thumbnail-width",
        type=int,
        default=DEFAULT_THUMBNAIL_WIDTH,
        help=f"Thumbnail image width. Default: {DEFAULT_THUMBNAIL_WIDTH}.",
    )
    visuals_parser.add_argument(
        "--include-non-content",
        action="store_true",
        help="Also extract visual assets for non-content scenes.",
    )
    visuals_parser.add_argument("--force", action="store_true", help="Overwrite existing extracted images.")

    text_parser = subparsers.add_parser(
        "detect-text",
        help="Run local OCR over extracted scene/event keyframes.",
    )
    text_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    text_parser.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    text_parser.add_argument(
        "--subject-type",
        choices=["all", "scene", "event"],
        default="all",
        help="Visual asset subject type to scan. Default: all.",
    )
    text_parser.add_argument(
        "--backend",
        choices=["auto", "apple-vision"],
        default=DEFAULT_TEXT_RECOGNITION_BACKEND,
        help="OCR backend. auto uses Apple Vision on macOS when available.",
    )
    text_parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_TEXT_MIN_CONFIDENCE,
        help=f"Minimum OCR confidence. Default: {DEFAULT_TEXT_MIN_CONFIDENCE}.",
    )
    text_parser.add_argument(
        "--languages",
        default="",
        help="Comma-separated recognition language hints such as en-US,ru-RU. Default: auto.",
    )
    text_parser.add_argument("--force", action="store_true", help="Replace matching OCR observations.")

    visual_embeddings_parser = subparsers.add_parser(
        "embed-visuals",
        help="Generate local image embeddings for extracted scene/event keyframes.",
    )
    visual_embeddings_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    visual_embeddings_parser.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    visual_embeddings_parser.add_argument(
        "--subject-type",
        choices=["all", "scene", "event"],
        default="all",
        help="Visual asset subject type to scan. Default: all.",
    )
    visual_embeddings_parser.add_argument(
        "--backend",
        choices=["auto", "sentence-transformers"],
        default=DEFAULT_VISUAL_EMBEDDING_BACKEND,
        help="Visual embedding backend. Default: auto.",
    )
    visual_embeddings_parser.add_argument(
        "--model",
        default=DEFAULT_VISUAL_EMBEDDING_MODEL,
        help=f"SentenceTransformers image model. Default: {DEFAULT_VISUAL_EMBEDDING_MODEL}.",
    )
    visual_embeddings_parser.add_argument("--force", action="store_true", help="Replace matching visual embeddings.")

    visual_captions_parser = subparsers.add_parser(
        "caption-visuals",
        help="Generate local captions for extracted scene/event keyframes.",
    )
    visual_captions_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    visual_captions_parser.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    visual_captions_parser.add_argument(
        "--subject-type",
        choices=["all", "scene", "event"],
        default="all",
        help="Visual asset subject type to scan. Default: all.",
    )
    visual_captions_parser.add_argument(
        "--backend",
        choices=["auto", "transformers-blip", "ollama"],
        default=DEFAULT_VISUAL_CAPTION_BACKEND,
        help="Visual caption backend. Default: auto.",
    )
    visual_captions_parser.add_argument(
        "--model",
        default=DEFAULT_VISUAL_CAPTION_MODEL,
        help=f"Caption model. Default: {DEFAULT_VISUAL_CAPTION_MODEL}.",
    )
    visual_captions_parser.add_argument(
        "--prompt",
        default="Describe the home-video frame, including setting, event, visible people, signs, and whether it looks like family footage.",
        help="Prompt used by instruction-following caption backends such as Ollama.",
    )
    visual_captions_parser.add_argument("--force", action="store_true", help="Replace matching visual captions.")

    visual_similarity_parser = subparsers.add_parser(
        "build-visual-similarity",
        help="Build local visual-similarity candidate edges from visual embeddings.",
    )
    visual_similarity_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    visual_similarity_parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.82,
        help="Minimum cosine similarity for candidate visual edges. Default: 0.82.",
    )
    visual_similarity_parser.add_argument(
        "--limit-per-asset",
        type=int,
        default=5,
        help="Maximum visual neighbors to keep per asset. Default: 5.",
    )

    content_classification_parser = subparsers.add_parser(
        "classify-content",
        help="Classify event content as likely family, unrelated, non-content, or uncertain.",
    )
    content_classification_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    content_classification_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.62,
        help="Minimum signal score for likely-family or likely-unrelated labels. Default: 0.62.",
    )

    faces_parser = subparsers.add_parser(
        "detect-faces",
        help="Detect local face thumbnails from extracted scene/event keyframes.",
    )
    faces_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    faces_parser.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    faces_parser.add_argument(
        "--subject-type",
        choices=["scene", "event"],
        default="scene",
        help="Visual asset subject type to scan. Default: scene.",
    )
    faces_parser.add_argument(
        "--min-size",
        type=int,
        default=DEFAULT_FACE_MIN_SIZE,
        help=f"Minimum face size in pixels. Default: {DEFAULT_FACE_MIN_SIZE}.",
    )
    faces_parser.add_argument(
        "--backend",
        choices=["auto", "opencv", "apple-vision"],
        default=DEFAULT_FACE_DETECTION_BACKEND,
        help="Face detector backend. auto uses Apple Vision on macOS when available, else OpenCV.",
    )
    faces_parser.add_argument("--force", action="store_true", help="Overwrite existing face thumbnails.")

    cluster_faces_parser = subparsers.add_parser(
        "cluster-faces",
        help="Cluster face thumbnails and create reviewable person identity candidates.",
    )
    cluster_faces_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    cluster_faces_parser.add_argument(
        "--max-distance",
        type=float,
        default=None,
        help=(
            "Maximum cosine distance for merging face thumbnails. "
            f"Default: backend-specific; OpenCV={DEFAULT_FACE_CLUSTER_DISTANCE}."
        ),
    )
    cluster_faces_parser.add_argument(
        "--embedding-backend",
        choices=["auto", "opencv-gray", "arcface-insightface"],
        default=DEFAULT_FACE_EMBEDDING_BACKEND,
        help="Face embedding backend. auto uses ArcFace/InsightFace when installed, else OpenCV grayscale.",
    )
    cluster_faces_parser.add_argument(
        "--min-cluster-size",
        type=int,
        default=1,
        help="Minimum faces per retained cluster. Default: 1.",
    )

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
    transcribe_local.add_argument(
        "--skip-existing",
        action="store_true",
        help="When transcribing multiple videos, skip sources that already have transcript segments.",
    )

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

    speaker_parser = subparsers.add_parser(
        "speakers",
        help="Run or import speaker diarization segments.",
    )
    speaker_subparsers = speaker_parser.add_subparsers(dest="speaker_command", required=True)
    speaker_diarize = speaker_subparsers.add_parser("diarize", help="Run local speaker diarization.")
    speaker_diarize.add_argument("project", type=Path, help="TapeSplit project directory.")
    speaker_diarize.add_argument("--source-video-id", help="Specific source video id. Defaults to all videos.")
    speaker_diarize.add_argument(
        "--backend",
        choices=["auto", "pyannote", "transcript-embedding", "speechbrain"],
        default=DEFAULT_SPEAKER_DIARIZATION_BACKEND,
        help="Speaker diarization backend. Default: auto.",
    )
    speaker_diarize.add_argument(
        "--model",
        default=DEFAULT_SPEAKER_DIARIZATION_MODEL,
        help=f"Diarization model. Default: {DEFAULT_SPEAKER_DIARIZATION_MODEL}.",
    )
    speaker_diarize.add_argument("--force", action="store_true", help="Replace matching speaker segments.")
    speaker_import = speaker_subparsers.add_parser("import", help="Import speaker segments from JSON or RTTM.")
    speaker_import.add_argument("project", type=Path, help="TapeSplit project directory.")
    speaker_import.add_argument("speaker_file", type=Path, help="JSON or RTTM speaker segment file.")
    speaker_import.add_argument("--source-video-id", required=True, help="Source video id for imported speaker segments.")
    speaker_import.add_argument(
        "--format",
        choices=["auto", "json", "rttm"],
        default="auto",
        help="Speaker file format. Default: auto.",
    )
    speaker_import.add_argument(
        "--offset-seconds",
        type=float,
        default=0.0,
        help="Add this source-time offset to imported speaker times. Default: 0.",
    )
    speaker_import.add_argument("--force", action="store_true", help="Replace existing speaker segments for this source.")
    speaker_identify = speaker_subparsers.add_parser(
        "identify",
        help="Infer reviewable person identity candidates for local speaker tracks.",
    )
    speaker_identify.add_argument("project", type=Path, help="TapeSplit project directory.")
    speaker_identify.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_SPEAKER_IDENTITY_CONFIDENCE,
        help=f"Minimum candidate confidence. Default: {DEFAULT_MIN_SPEAKER_IDENTITY_CONFIDENCE}.",
    )
    speaker_identify.add_argument(
        "--max-candidates-per-speaker",
        type=int,
        default=DEFAULT_MAX_CANDIDATES_PER_SPEAKER,
        help=f"Maximum candidates to keep per speaker. Default: {DEFAULT_MAX_CANDIDATES_PER_SPEAKER}.",
    )

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

    build_place_roles_parser = subparsers.add_parser(
        "build-place-roles",
        help="Classify raw event place mentions into filming, context, travel-plan, and ambiguous roles.",
    )
    build_place_roles_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    build_place_roles_parser.add_argument(
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

    agent_parser = subparsers.add_parser(
        "agent",
        help="Run local agent-style planning and alignment passes.",
    )
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_align = agent_subparsers.add_parser(
        "align",
        help="Align canonical events against transcript/evidence support.",
    )
    agent_align.add_argument("project", type=Path, help="TapeSplit project directory.")
    agent_align.add_argument(
        "--context-seconds",
        type=float,
        default=45.0,
        help="Transcript context window around event ranges. Default: 45.",
    )
    agent_reconcile = agent_subparsers.add_parser(
        "reconcile",
        help="Reconcile event titles and metadata from aligned local evidence.",
    )
    agent_reconcile.add_argument("project", type=Path, help="TapeSplit project directory.")

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="Rebuild derived events, groups, graph, search, report, and visualization artifacts.",
    )
    rebuild_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    rebuild_parser.add_argument(
        "--import-gemini",
        action="store_true",
        help="Import Gemini analyses before rebuilding derived artifacts.",
    )
    rebuild_parser.add_argument("--gemini-run-id", help="Import this Gemini analysis_run_id.")
    rebuild_parser.add_argument(
        "--import-all-gemini",
        action="store_true",
        help="Import the best available Gemini analysis per source video instead of the latest run.",
    )
    rebuild_parser.add_argument(
        "--include-duplicate-gemini-runs",
        action="store_true",
        help="With --import-all-gemini, import every Gemini analysis row including older duplicate runs.",
    )
    rebuild_parser.add_argument(
        "--max-gap-seconds",
        type=float,
        default=120.0,
        help="Maximum gap for merging related boundary-split events. Default: 120.",
    )
    rebuild_parser.add_argument(
        "--include-legacy-events",
        action="store_true",
        help="Include non-Gemini event candidates even when Gemini events exist.",
    )
    rebuild_parser.add_argument(
        "--alignment-context-seconds",
        type=float,
        default=45.0,
        help="Transcript context window for event alignment. Default: 45.",
    )
    rebuild_parser.add_argument(
        "--relationship-context-seconds",
        type=float,
        default=8.0,
        help="Transcript context window around kinship terms. Default: 8.",
    )
    rebuild_parser.add_argument(
        "--embedding-backend",
        choices=["local-sparse", "sentence-transformers", "auto"],
        default="local-sparse",
        help="Semantic embedding backend. Default: local-sparse.",
    )
    rebuild_parser.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="SentenceTransformers model when --embedding-backend sentence-transformers/auto is used.",
    )
    rebuild_parser.add_argument("--no-report", action="store_true", help="Skip review.html export.")
    rebuild_parser.add_argument("--no-visualization", action="store_true", help="Skip visualization.json export.")

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
            "event_alignment",
            "event_reconciliation",
            "event_continuity_context",
            "event_group",
            "evidence",
            "language_group",
            "people_group",
            "place_group",
            "relationship_candidate",
            "relationship_review_task",
            "scene",
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

    review_parser = subparsers.add_parser(
        "review",
        help="Apply durable review corrections to project artifacts.",
    )
    review_subparsers = review_parser.add_subparsers(dest="review_command", required=True)
    review_apply = review_subparsers.add_parser(
        "apply",
        help="Apply review actions from a JSON or JSONL file and append corrections.jsonl.",
    )
    review_apply.add_argument("project", type=Path, help="TapeSplit project directory.")
    review_apply.add_argument("actions", type=Path, help="JSON/JSONL review action file.")
    review_apply_suggestions = review_subparsers.add_parser(
        "apply-suggestions",
        help="Apply review_queue suggested actions in bulk.",
    )
    review_apply_suggestions.add_argument("project", type=Path, help="TapeSplit project directory.")
    review_apply_suggestions.add_argument(
        "--tier",
        choices=["primary", "backlog", "all"],
        default="primary",
        help="Which suggestion tier to apply.",
    )
    review_apply_suggestions.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="Only apply suggestions at or above this confidence.",
    )
    review_apply_suggestions.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected actions without writing corrections.",
    )
    review_apply_suggestions.add_argument(
        "--reviewer",
        default="bulk-suggestion",
        help="Reviewer label to store on generated corrections.",
    )
    review_apply_suggestions.add_argument(
        "--policy",
        choices=["safe", "legacy"],
        default="safe",
        help="safe: per-action confidence floors + relationship corroboration gates; legacy: tier/min-confidence only.",
    )
    review_calibrate = review_subparsers.add_parser(
        "calibrate",
        help="Tune safe-policy confidence floors from this project's review outcomes.",
    )
    review_calibrate.add_argument("project", type=Path, help="TapeSplit project directory.")
    review_calibrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the tuned policy without writing review_policy.json.",
    )
    review_reapply = review_subparsers.add_parser(
        "reapply",
        help="Replay existing corrections.jsonl against regenerated project artifacts.",
    )
    review_reapply.add_argument("project", type=Path, help="TapeSplit project directory.")
    review_reapply.add_argument(
        "--strict",
        action="store_true",
        help="Fail when a correction target is missing instead of reporting it as skipped.",
    )
    review_list = review_subparsers.add_parser(
        "list",
        help="List durable review corrections for a project.",
    )
    review_list.add_argument("project", type=Path, help="TapeSplit project directory.")

    report_parser = subparsers.add_parser(
        "export-report",
        help="Export a static review.html report.",
    )
    report_parser.add_argument("project", type=Path, help="TapeSplit project directory.")

    story_parser = subparsers.add_parser(
        "export-story",
        help="Export a grounded JSON/Markdown tape story from reviewed local artifacts.",
    )
    story_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    story_parser.add_argument("--json-out", type=Path, help="Output JSON path. Defaults to <project>/story.json.")
    story_parser.add_argument("--md-out", type=Path, help="Output Markdown path. Defaults to <project>/tape_story.md.")

    viz_parser = subparsers.add_parser(
        "export-visualization",
        help="Export UI-ready timeline, place, people, relationship, and asset data.",
    )
    viz_parser.add_argument("project", type=Path, help="TapeSplit project directory.")
    viz_parser.add_argument(
        "--out",
        type=Path,
        help="Output JSON path. Defaults to <project>/visualization.json.",
    )

    return parser


def _run_ui(project_dir: Path, *, port: int | None = None) -> int:
    import os
    import subprocess

    project = project_dir.expanduser().resolve()
    if not (project / "visualization.json").exists():
        print(
            f"warning: {project / 'visualization.json'} does not exist yet; "
            "run `tapesplit auto` or `tapesplit export-visualization` first.",
            file=sys.stderr,
        )
    repo_root = Path(__file__).resolve().parents[2]
    ui_dir = repo_root / "apps" / "review-ui"
    if not (ui_dir / "package.json").exists():
        print(
            f"error: review UI not found at {ui_dir}; "
            "`tapesplit ui` requires a repo checkout (editable install).",
            file=sys.stderr,
        )
        return 1
    if shutil.which("npm") is None:
        print("error: npm is required to run the review UI (brew install node).", file=sys.stderr)
        return 1
    if not (ui_dir / "node_modules").exists():
        print("installing review UI dependencies (first run)…")
        install = subprocess.run(["npm", "install"], cwd=ui_dir)
        if install.returncode != 0:
            return install.returncode

    env = dict(os.environ)
    env["TAPESPLIT_PROJECT"] = str(project)
    command = ["npm", "run", "dev", "--", "--host", "127.0.0.1"]
    if port:
        command.extend(["--port", str(port)])
    print(f"starting review UI for {project} (Ctrl-C to stop)…")
    try:
        return subprocess.call(command, cwd=ui_dir, env=env)
    except KeyboardInterrupt:
        return 0


def _doctor(as_json: bool) -> int:
    status = check_twelvelabs_config()
    status.update(check_azure_openai_config())
    status.update(check_google_maps_config())
    status.update(check_gemini_config())
    status.update(check_transcription_config())
    status.update(check_face_detection_config())
    status.update(check_face_embedding_config())
    status.update(check_visual_text_config())
    status.update(check_visual_embedding_config())
    status.update(check_visual_caption_config())
    status.update(check_speaker_diarization_config())
    status["ffprobe"] = shutil.which("ffprobe") is not None
    status["ffmpeg"] = shutil.which("ffmpeg") is not None
    status["exiftool"] = shutil.which("exiftool") is not None
    status["npm"] = shutil.which("npm") is not None
    try:
        import sentence_transformers  # noqa: F401

        status["search_dense_embeddings"] = True
    except ImportError:
        status["search_dense_embeddings"] = False

    from tapesplit.auto import build_stages, gather_capabilities

    capabilities = gather_capabilities()
    stage_availability = {}
    for stage in build_stages():
        if stage.availability is None:
            stage_availability[stage.name] = {"available": True, "reason": ""}
        else:
            ok, reason = stage.availability(capabilities)
            stage_availability[stage.name] = {"available": ok, "reason": reason}
    status["pipeline_stages"] = stage_availability

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
        print(f"  OpenCV face detection: {'installed' if status['opencv'] else 'missing'}")
        print(f"  Apple Vision face detection: {'installed' if status['apple_vision'] else 'missing'}")
        print(f"  Default face detection backend: {status['face_detection_default_backend']}")
        print(f"  ArcFace face embeddings: {'installed' if status['face_embedding_arcface'] else 'missing'}")
        print(f"  Default face embedding backend: {status['face_embedding_default_backend']}")
        print(f"  Apple Vision OCR: {'installed' if status['apple_vision_ocr'] else 'missing'}")
        print(f"  Default visual text backend: {status['visual_text_default_backend']}")
        print(
            "  Visual image embeddings: "
            f"{'installed' if status['visual_embedding_sentence_transformers'] else 'missing'}"
        )
        print(f"  Default visual embedding backend: {status['visual_embedding_default_backend']}")
        print(f"  Visual caption transformers: {'installed' if status['visual_caption_transformers'] else 'missing'}")
        print(f"  Visual caption Ollama: {'installed' if status['visual_caption_ollama'] else 'missing'}")
        print(f"  Default visual caption backend: {status['visual_caption_default_backend']}")
        print(f"  Speaker diarization pyannote: {'installed' if status['speaker_diarization_pyannote'] else 'missing'}")
        print(f"  Speaker diarization HF token: {'configured' if status['speaker_diarization_hf_token'] else 'missing'}")
        print(
            "  Speaker diarization speechbrain fallback: "
            f"{'installed' if status['speaker_diarization_speechbrain'] else 'missing'}"
        )
        print(f"  ExifTool: {'installed' if status['exiftool'] else 'missing'}")
        print(f"  npm (review UI): {'installed' if status['npm'] else 'missing'}")
        print(
            "  Search dense embeddings: "
            f"{'installed' if status['search_dense_embeddings'] else 'missing (local-sparse fallback)'}"
        )
        print()
        print("  tapesplit auto stage availability:")
        for name, info in status["pipeline_stages"].items():
            if info["available"]:
                print(f"    {name:<18} ready")
            else:
                print(f"    {name:<18} unavailable — {info['reason']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "auto":
            options = AutoOptions(
                profile=args.profile,
                out=args.out,
                window_seconds=args.window_seconds,
                language=args.language,
                force=args.force,
                force_from=args.force_from,
                skip=tuple(args.skip),
                only=tuple(args.only),
                max_cloud_usd=None if args.max_cloud_usd is not None and args.max_cloud_usd < 0 else args.max_cloud_usd,
                suggestions_tier=None if args.no_suggestions else args.suggestions_tier,
                suggestions_min_confidence=args.suggestions_min_confidence,
                search_embedding_backend=args.search_embedding_backend,
                plan_only=args.plan,
                as_json=args.json,
                quiet=args.json,
            )
            result = run_auto(args.input, options)
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if (args.plan or result.get("ok")) else 1
        if args.command == "status":
            status = project_status(args.project)
            if args.json:
                print(json.dumps(status, indent=2, sort_keys=True))
                return 0
            print(f"TapeSplit project: {status['project']}")
            print("  stages:")
            for row in status["stages"]:
                duration = f" ({row['duration_s']}s)" if row.get("duration_s") else ""
                reason = f" — {row['reason']}" if row.get("reason") else ""
                print(f"    {row['stage']:<18} {row['status']}{duration}{reason}")
            print("  metrics:")
            for key, value in sorted(status["metrics"].items()):
                if value not in (None, 0, 0.0, []):
                    print(f"    {key}: {value}")
            return 0
        if args.command == "ui":
            return _run_ui(args.project, port=args.port)
        if args.command == "synthesize-events":
            print(
                json.dumps(
                    build_heuristic_events(
                        args.project,
                        only_uncovered_sources=not args.include_covered,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
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
                if args.all or args.uploaded_to_twelvelabs_only:
                    print(
                        json.dumps(
                            estimate_project_videos(
                                args.project,
                                source_video_id=args.source_video_id,
                                all_videos=args.all,
                                uploaded_to_twelvelabs_only=args.uploaded_to_twelvelabs_only,
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
            if args.gemini_command == "prepare-video":
                print(
                    json.dumps(
                        prepare_project_video_proxies(
                            args.project,
                            source_video_id=args.source_video_id,
                            force=args.force,
                            max_upload_bytes=int(args.max_upload_gb * 1_000_000_000),
                            target_height=args.height,
                            target_fps=args.proxy_fps,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "analyze-video":
                if args.all or args.uploaded_to_twelvelabs_only:
                    print(
                        json.dumps(
                            analyze_project_videos(
                                args.project,
                                source_video_id=args.source_video_id,
                                all_videos=args.all,
                                uploaded_to_twelvelabs_only=args.uploaded_to_twelvelabs_only,
                                continue_on_error=args.continue_on_error,
                                fps=args.fps,
                                media_resolution=args.media_resolution,
                                max_output_tokens=args.max_output_tokens,
                                force_upload=args.force_upload,
                                use_proxy=not args.no_proxy,
                                force_proxy=args.force_proxy,
                                max_upload_bytes=int(args.max_upload_gb * 1_000_000_000),
                                proxy_height=args.proxy_height,
                                proxy_fps=args.proxy_fps,
                            ),
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    return 0
                print(
                    json.dumps(
                        analyze_project_video(
                            args.project,
                            source_video_id=args.source_video_id,
                            fps=args.fps,
                            media_resolution=args.media_resolution,
                            max_output_tokens=args.max_output_tokens,
                            force_upload=args.force_upload,
                            use_proxy=not args.no_proxy,
                            force_proxy=args.force_proxy,
                            max_upload_bytes=int(args.max_upload_gb * 1_000_000_000),
                            proxy_height=args.proxy_height,
                            proxy_fps=args.proxy_fps,
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
                        import_gemini_analysis(
                            args.project,
                            run_id=args.run_id,
                            all_runs=args.all,
                            best_per_source=not args.include_duplicate_runs,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "summarize-analyses":
                print(
                    json.dumps(
                        summarize_gemini_analyses(args.project, source_video_id=args.source_video_id),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.gemini_command == "compare-analyses":
                print(
                    json.dumps(
                        compare_gemini_analysis_modes(args.project, source_video_id=args.source_video_id),
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
        if args.command == "detect-scenes":
            print(
                json.dumps(
                    detect_scenes_for_project(
                        args.project,
                        source_video_id=args.source_video_id,
                        threshold=args.threshold,
                        min_scene_seconds=args.min_scene_seconds,
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
        if args.command == "extract-visuals":
            print(
                json.dumps(
                    extract_visual_assets_for_project(
                        args.project,
                        subjects=[item.strip() for item in args.subjects.split(",") if item.strip()],
                        source_video_id=args.source_video_id,
                        keyframe_width=args.keyframe_width,
                        thumbnail_width=args.thumbnail_width,
                        include_non_content=args.include_non_content,
                        force=args.force,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "detect-text":
            print(
                json.dumps(
                    detect_text_for_project(
                        args.project,
                        source_video_id=args.source_video_id,
                        subject_type=args.subject_type,
                        backend=args.backend,
                        min_confidence=args.min_confidence,
                        languages=[item.strip() for item in args.languages.split(",") if item.strip()],
                        force=args.force,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "embed-visuals":
            print(
                json.dumps(
                    embed_visual_assets_for_project(
                        args.project,
                        source_video_id=args.source_video_id,
                        subject_type=args.subject_type,
                        backend=args.backend,
                        model_name=args.model,
                        force=args.force,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "caption-visuals":
            print(
                json.dumps(
                    caption_visual_assets_for_project(
                        args.project,
                        source_video_id=args.source_video_id,
                        subject_type=args.subject_type,
                        backend=args.backend,
                        model_name=args.model,
                        prompt=args.prompt,
                        force=args.force,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "build-visual-similarity":
            print(
                json.dumps(
                    build_visual_similarity_for_project(
                        args.project,
                        min_similarity=args.min_similarity,
                        limit_per_asset=args.limit_per_asset,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "classify-content":
            print(
                json.dumps(
                    build_content_classifications_for_project(
                        args.project,
                        confidence_threshold=args.confidence_threshold,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "detect-faces":
            print(
                json.dumps(
                    detect_face_thumbnails_for_project(
                        args.project,
                        source_video_id=args.source_video_id,
                        subject_type=args.subject_type,
                        min_size=args.min_size,
                        backend=args.backend,
                        force=args.force,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "cluster-faces":
            print(
                json.dumps(
                    cluster_faces_for_project(
                        args.project,
                        max_distance=args.max_distance,
                        min_cluster_size=args.min_cluster_size,
                        embedding_backend=args.embedding_backend,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
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
                            skip_existing=args.skip_existing,
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
        if args.command == "speakers":
            if args.speaker_command == "diarize":
                print(
                    json.dumps(
                        diarize_project_speakers(
                            args.project,
                            source_video_id=args.source_video_id,
                            backend=args.backend,
                            model_name=args.model,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.speaker_command == "import":
                print(
                    json.dumps(
                        import_speaker_segments(
                            args.project,
                            args.speaker_file,
                            source_video_id=args.source_video_id,
                            speaker_format=args.format,
                            offset_seconds=args.offset_seconds,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.speaker_command == "identify":
                print(
                    json.dumps(
                        build_speaker_identity_candidates(
                            args.project,
                            min_confidence=args.min_confidence,
                            max_candidates_per_speaker=args.max_candidates_per_speaker,
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
        if args.command == "build-place-roles":
            print(
                json.dumps(
                    build_place_roles_for_project(
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
        if args.command == "agent":
            if args.agent_command == "align":
                print(
                    json.dumps(
                        build_event_alignments(args.project, context_seconds=args.context_seconds),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.agent_command == "reconcile":
                print(json.dumps(build_event_reconciliations(args.project), indent=2, sort_keys=True))
                return 0
        if args.command == "rebuild":
            print(
                json.dumps(
                    rebuild_project_outputs(
                        args.project,
                        import_gemini=args.import_gemini,
                        gemini_run_id=args.gemini_run_id,
                        import_all_gemini=args.import_all_gemini,
                        include_duplicate_gemini_runs=args.include_duplicate_gemini_runs,
                        max_gap_seconds=args.max_gap_seconds,
                        include_legacy_events=args.include_legacy_events,
                        alignment_context_seconds=args.alignment_context_seconds,
                        relationship_context_seconds=args.relationship_context_seconds,
                        embedding_backend=args.embedding_backend,
                        embedding_model=args.embedding_model,
                        export_report=not args.no_report,
                        export_visualization=not args.no_visualization,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
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
        if args.command == "review":
            if args.review_command == "apply":
                print(
                    json.dumps(
                        apply_review_actions(args.project, actions_path=args.actions),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.review_command == "apply-suggestions":
                print(
                    json.dumps(
                        apply_review_suggestions(
                            args.project,
                            tier=args.tier,
                            min_confidence=args.min_confidence,
                            dry_run=args.dry_run,
                            reviewer=args.reviewer,
                            policy=args.policy,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.review_command == "calibrate":
                from tapesplit.calibration import calibrate_review_policy

                print(
                    json.dumps(
                        calibrate_review_policy(args.project, write=not args.dry_run),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.review_command == "reapply":
                print(
                    json.dumps(
                        reapply_review_corrections(args.project, strict=args.strict),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            if args.review_command == "list":
                print(json.dumps(list_review_corrections(args.project), indent=2, sort_keys=True))
                return 0
        if args.command == "export-report":
            print(json.dumps(export_review_report(args.project), indent=2, sort_keys=True))
            return 0
        if args.command == "export-story":
            print(
                json.dumps(
                    export_story(args.project, out_json=args.json_out, out_md=args.md_out),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "export-visualization":
            print(
                json.dumps(
                    export_visualization_data(args.project, out_path=args.out),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any

from tapesplit.costs import ApiUsage, append_api_usage, estimate_api_cost_usd
from tapesplit.env import load_dotenv
from tapesplit.storage import append_jsonl, read_jsonl, write_json


DIRECT_UPLOAD_LIMIT_BYTES = 200 * 1024 * 1024


def check_twelvelabs_config(env_path: Path | None = None) -> dict:
    load_dotenv(env_path)
    return {
        "twelvelabs_api_key": bool(os.environ.get("TWELVELABS_API_KEY")),
        "twelvelabs_sdk": importlib.util.find_spec("twelvelabs") is not None,
    }


def require_twelvelabs_client(env_path: Path | None = None) -> Any:
    load_dotenv(env_path)
    api_key = os.environ.get("TWELVELABS_API_KEY")
    if not api_key:
        raise RuntimeError("TWELVELABS_API_KEY is not configured")
    if importlib.util.find_spec("twelvelabs") is None:
        raise RuntimeError("TwelveLabs SDK is not installed. Run: pip install -e '.[twelvelabs]'")

    from twelvelabs import TwelveLabs

    return TwelveLabs(api_key=api_key)


def create_index(index_name: str, include_pegasus: bool = False) -> dict:
    from twelvelabs.indexes.types import IndexesCreateRequestModelsItem

    client = require_twelvelabs_client()
    models = [
        IndexesCreateRequestModelsItem(
            model_name="marengo3.0",
            model_options=["visual", "audio"],
        )
    ]
    if include_pegasus:
        models.append(
            IndexesCreateRequestModelsItem(
                model_name="pegasus1.2",
                model_options=["visual", "audio"],
            )
        )
    response = client.indexes.create(index_name=index_name, models=models, addons=["thumbnail"])
    return _serialize(response)


def list_indexes() -> list[dict]:
    client = require_twelvelabs_client()
    response = client.indexes.list()
    return [_serialize(item) for item in response]


def upload_project_videos(
    project_dir: Path,
    index_id: str,
    wait: bool = False,
    limit: int | None = None,
    source_video_ids: list[str] | None = None,
) -> dict:
    project = project_dir.expanduser().resolve()
    tapes_path = project / "tapes.jsonl"
    if not tapes_path.exists():
        raise FileNotFoundError(f"missing project tapes file: {tapes_path}")

    client = require_twelvelabs_client()
    videos = read_jsonl(tapes_path)
    if source_video_ids:
        requested = set(source_video_ids)
        found = {video.get("id") for video in videos}
        missing = sorted(requested - found)
        if missing:
            raise ValueError(f"source video ids not found in project: {', '.join(missing)}")
        videos = [video for video in videos if video.get("id") in requested]
    if limit is not None:
        videos = videos[:limit]

    results = []
    for video in videos:
        video_path = Path(video["path"])
        if not video_path.exists():
            raise FileNotFoundError(f"video no longer exists: {video_path}")
        metadata = {
            "tapesplit_source_video_id": video["id"],
            "filename": video["filename"],
        }
        upload_method = "direct_task"
        asset = None
        indexed_asset = None
        if video_path.stat().st_size > DIRECT_UPLOAD_LIMIT_BYTES:
            upload_method = "multipart_asset"
            asset = client.multipart_upload.upload_file(
                video_path,
                filename=video["filename"],
                file_type="video",
            )
            append_jsonl(
                project / "twelvelabs_assets.jsonl",
                {
                    "source_video_id": video["id"],
                    "filename": video["filename"],
                    "asset": _sanitize_twelvelabs_payload(_serialize(asset)),
                },
            )
            indexed_asset = client.indexes.indexed_assets.create(
                index_id,
                asset_id=asset.asset_id,
                enable_video_stream=True,
                user_metadata=metadata,
            )
            task = indexed_asset
        else:
            task = client.tasks.create(
                index_id=index_id,
                video_file=str(video_path),
                user_metadata=json.dumps(metadata, sort_keys=True),
            )
        task_record = {
            "source_video_id": video["id"],
            "filename": video["filename"],
            "index_id": index_id,
            "upload_method": upload_method,
            "asset": _sanitize_twelvelabs_payload(_serialize(asset)) if asset is not None else None,
            "indexed_asset": _serialize(indexed_asset) if indexed_asset is not None else None,
            "task": _serialize(task),
        }
        append_jsonl(project / "twelvelabs_tasks.jsonl", task_record)
        duration_s = float((video.get("probe") or {}).get("duration_s") or 0.0)
        duration_min = round(duration_s / 60.0, 6)
        append_api_usage(
            project,
            ApiUsage(
                provider="twelvelabs",
                service="index",
                operation="index_video",
                units={
                    "duration_min": duration_min,
                    "requests": 1,
                },
                estimated_cost_usd=estimate_api_cost_usd(
                    provider="twelvelabs",
                    service="index",
                    units={
                        "duration_min": duration_min,
                        "requests": 1,
                    },
                ),
                request_id=_get_id(task),
                metadata={
                    "source_video_id": video["id"],
                    "filename": video["filename"],
                    "index_id": index_id,
                    "upload_method": upload_method,
                },
            ),
        )

        if wait:
            task_id = _get_id(task)
            if not task_id:
                raise RuntimeError(f"could not determine TwelveLabs task/indexed asset id for {video_path}")
            if upload_method == "multipart_asset":
                done = _wait_for_indexed_asset(client, index_id, task_id)
            else:
                done = client.tasks.wait_for_done(task_id)
            done_record = {
                "source_video_id": video["id"],
                "filename": video["filename"],
                "index_id": index_id,
                "task": _serialize(done),
            }
            append_jsonl(project / "twelvelabs_tasks.done.jsonl", done_record)
            task_record["done"] = done_record["task"]

        results.append(task_record)

    write_json(
        project / "twelvelabs_index.json",
        {
            "index_id": index_id,
            "uploaded_video_count": len(results),
        },
    )
    return {
        "project": str(project),
        "index_id": index_id,
        "uploaded_video_count": len(results),
        "waited": wait,
    }


def project_index_status(project_dir: Path) -> dict:
    project = project_dir.expanduser().resolve()
    index_file = project / "twelvelabs_index.json"
    if not index_file.exists():
        raise FileNotFoundError(f"missing TwelveLabs index file: {index_file}")
    index_id = json.loads(index_file.read_text(encoding="utf-8"))["index_id"]
    client = require_twelvelabs_client()
    assets = []
    for row in read_jsonl(project / "twelvelabs_tasks.jsonl"):
        indexed_asset = row.get("indexed_asset") or row.get("task") or {}
        indexed_asset_id = indexed_asset.get("id")
        if not indexed_asset_id:
            continue
        asset = client.indexes.indexed_assets.retrieve(index_id, indexed_asset_id)
        assets.append(_sanitize_twelvelabs_payload(_serialize(asset)))
    return {
        "project": str(project),
        "index_id": index_id,
        "assets": assets,
    }


def search_project(
    project_dir: Path,
    query: str,
    page_limit: int = 10,
    search_options: list[str] | None = None,
) -> dict:
    project = project_dir.expanduser().resolve()
    index_file = project / "twelvelabs_index.json"
    if not index_file.exists():
        raise FileNotFoundError(f"missing TwelveLabs index file: {index_file}")
    index_id = json.loads(index_file.read_text(encoding="utf-8"))["index_id"]
    client = require_twelvelabs_client()
    options = search_options or ["visual", "audio"]
    response = client.search.query(
        index_id=index_id,
        query_text=query,
        search_options=options,
        page_limit=page_limit,
    )
    items = [_sanitize_twelvelabs_payload(_serialize(item)) for _, item in zip(range(page_limit), response)]
    append_api_usage(
        project,
        ApiUsage(
            provider="twelvelabs",
            service="search",
            operation="search_query",
            units={"requests": 1},
            estimated_cost_usd=estimate_api_cost_usd(
                provider="twelvelabs",
                service="search",
                units={"requests": 1},
            ),
            metadata={
                "index_id": index_id,
                "query": query,
                "page_limit": page_limit,
                "search_options": options,
            },
        ),
    )
    append_jsonl(
        project / "twelvelabs_searches.jsonl",
        {
            "index_id": index_id,
            "query": query,
            "page_limit": page_limit,
            "search_options": options,
            "results": items,
        },
    )
    return {
        "project": str(project),
        "index_id": index_id,
        "query": query,
        "results": items,
    }


def _wait_for_indexed_asset(client: Any, index_id: str, indexed_asset_id: str) -> Any:
    terminal = {"ready", "failed"}
    while True:
        asset = client.indexes.indexed_assets.retrieve(index_id, indexed_asset_id)
        status = getattr(asset, "status", None)
        if status in terminal:
            return asset
        time.sleep(5)


def _get_id(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("id") or value.get("_id") or value.get("task_id")
    return getattr(value, "id", None) or getattr(value, "_id", None) or getattr(value, "task_id", None)


def _serialize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _serialize(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _sanitize_twelvelabs_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_twelvelabs_payload(item)
            for key, item in value.items()
            if key not in {"asset_url", "video_url", "thumbnail_url", "thumbnail_urls"}
        }
    if isinstance(value, list):
        return [_sanitize_twelvelabs_payload(item) for item in value]
    return value

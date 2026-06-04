from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tapesplit.costs import LlmUsage, append_llm_usage, estimate_llm_cost_usd
from tapesplit.env import load_dotenv


@dataclass(frozen=True)
class AzureOpenAIConfig:
    endpoint: str | None
    api_key: str | None
    region: str | None
    api_version: str | None
    reasoning_deployment: str | None
    fast_deployment: str | None
    full_deployment: str | None
    chat_deployment: str | None
    codex_deployment: str | None
    whisper_deployment: str | None

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.api_version)


def load_azure_openai_config(env_path: Path | None = None) -> AzureOpenAIConfig:
    load_dotenv(env_path)
    return AzureOpenAIConfig(
        endpoint=_empty_to_none(os.environ.get("AZURE_OPENAI_ENDPOINT")),
        api_key=_empty_to_none(os.environ.get("AZURE_OPENAI_API_KEY")),
        region=_empty_to_none(os.environ.get("AZURE_OPENAI_REGION")),
        api_version=_empty_to_none(os.environ.get("AZURE_OPENAI_API_VERSION")),
        reasoning_deployment=_empty_to_none(os.environ.get("AZURE_OPENAI_REASONING_DEPLOYMENT")),
        fast_deployment=_empty_to_none(os.environ.get("AZURE_OPENAI_FAST_DEPLOYMENT")),
        full_deployment=_empty_to_none(os.environ.get("AZURE_OPENAI_FULL_DEPLOYMENT")),
        chat_deployment=_empty_to_none(os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")),
        codex_deployment=_empty_to_none(os.environ.get("AZURE_OPENAI_CODEX_DEPLOYMENT")),
        whisper_deployment=_empty_to_none(os.environ.get("AZURE_OPENAI_WHISPER_DEPLOYMENT")),
    )


def check_azure_openai_config(env_path: Path | None = None) -> dict:
    config = load_azure_openai_config(env_path)
    return {
        "azure_openai_endpoint": bool(config.endpoint),
        "azure_openai_api_key": bool(config.api_key),
        "azure_openai_region": config.region,
        "azure_openai_api_version": config.api_version,
        "azure_openai_configured": config.configured,
        "azure_openai_deployments": {
            "reasoning": config.reasoning_deployment,
            "fast": config.fast_deployment,
            "full": config.full_deployment,
            "chat": config.chat_deployment,
            "codex": config.codex_deployment,
            "whisper": config.whisper_deployment,
        },
    }


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def deployment_for_alias(config: AzureOpenAIConfig, alias: str) -> str:
    aliases = {
        "reasoning": config.reasoning_deployment,
        "fast": config.fast_deployment,
        "full": config.full_deployment,
        "chat": config.chat_deployment,
        "codex": config.codex_deployment,
        "whisper": config.whisper_deployment,
    }
    deployment = aliases.get(alias, alias)
    if not deployment:
        raise RuntimeError(f"Azure OpenAI deployment is not configured for alias: {alias}")
    return deployment


def chat_completion(
    *,
    deployment: str,
    messages: list[dict[str, str]],
    max_tokens: int = 64,
    temperature: float = 0.0,
    response_format: dict[str, Any] | None = None,
    project_dir: Path | None = None,
    operation: str = "chat_completion",
) -> dict[str, Any]:
    config = load_azure_openai_config()
    if not config.configured:
        raise RuntimeError("Azure OpenAI endpoint, API key, and API version must be configured")

    endpoint = config.endpoint.rstrip("/")
    url = (
        f"{endpoint}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={config.api_version}"
    )
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "api-key": config.api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            request_id = response.headers.get("x-request-id") or response.headers.get("apim-request-id")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Azure OpenAI request failed ({exc.code}): {detail}") from exc

    result = json.loads(body)
    usage = result.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached_input_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    if project_dir is not None:
        append_llm_usage(
            project_dir,
            LlmUsage(
                provider="azure_openai",
                deployment=deployment,
                operation=operation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                estimated_cost_usd=estimate_llm_cost_usd(
                    provider="azure_openai",
                    deployment=deployment,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_input_tokens=cached_input_tokens,
                ),
                request_id=request_id,
            ),
        )
    return result


def smoke_test(alias_or_deployment: str = "fast", project_dir: Path | None = None) -> dict[str, Any]:
    config = load_azure_openai_config()
    deployment = deployment_for_alias(config, alias_or_deployment)
    response = chat_completion(
        deployment=deployment,
        messages=[
            {"role": "system", "content": "Return only compact JSON."},
            {"role": "user", "content": "Return {\"ok\": true, \"service\": \"azure_openai\"}."},
        ],
        max_tokens=32,
        project_dir=project_dir,
        operation="smoke_test",
    )
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "deployment": deployment,
        "content": message.get("content"),
        "usage": response.get("usage"),
    }

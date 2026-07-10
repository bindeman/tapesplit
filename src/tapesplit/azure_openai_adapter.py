from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tapesplit.costs import LlmUsage, append_llm_usage, estimate_llm_cost_usd
from tapesplit.env import load_dotenv


DEFAULT_DIARIZE_DEPLOYMENT = "gpt-4o-transcribe-diarize"
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class ContentPolicyViolation(RuntimeError):
    """Azure's pre-inference content filter refused the request.

    Raised distinctly so callers can bisect frame batches: the filter is
    known to refuse innocuous home-video frames (bath/potty scenes), and the
    correct response is dropping the flagged frames, not failing the claim.
    """


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
        # AZURE_OPENAI_API_BASE is the alias used by adjacent tooling
        # (LiteLLM/openai-python); accept either name.
        endpoint=_empty_to_none(
            os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get("AZURE_OPENAI_API_BASE")
        ),
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


def reasoning_chat_completion(
    *,
    deployment: str,
    messages: list[dict[str, Any]],
    max_completion_tokens: int = 1200,
    reasoning_effort: str = "medium",
    response_format: dict[str, Any] | None = None,
    project_dir: Path | None = None,
    operation: str = "reasoning_chat_completion",
    timeout: int = 240,
    max_attempts: int = 4,
) -> dict[str, Any]:
    """Chat completion for the gpt-5.6 reasoning family (sol/terra/luna).

    These deployments reject ``max_tokens`` and ``temperature``; they take
    ``max_completion_tokens`` and ``reasoning_effort``. Messages may carry
    multimodal content parts (``text`` / ``image_url``). Raises
    :class:`ContentPolicyViolation` when the pre-inference filter refuses the
    input so callers can bisect frame batches.
    """

    config = load_azure_openai_config()
    if not config.configured:
        raise RuntimeError("Azure OpenAI endpoint, API key, and API version must be configured")

    endpoint = config.endpoint.rstrip("/")
    url = (
        f"{endpoint}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={config.api_version}"
    )
    payload: dict[str, Any] = {
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": reasoning_effort,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    data = json.dumps(payload).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        request = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "api-key": config.api_key},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "apim-request-id"
                )
            break
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if "content_policy_violation" in detail or "content safety" in detail.lower():
                raise ContentPolicyViolation(detail[:400]) from exc
            last_error = RuntimeError(f"Azure OpenAI request failed ({exc.code}): {detail[:400]}")
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == max_attempts:
                raise last_error from exc
            time.sleep(15.0 * attempt)
        except (TimeoutError, URLError, ConnectionError, HTTPException) as exc:
            last_error = RuntimeError(f"Azure OpenAI request failed ({type(exc).__name__}): {exc}")
            if attempt == max_attempts:
                raise last_error from exc
            time.sleep(15.0 * attempt)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_error or RuntimeError("Azure OpenAI request failed")

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


def transcribe_diarize(
    *,
    audio_path: Path,
    deployment: str = DEFAULT_DIARIZE_DEPLOYMENT,
    known_speakers: dict[str, bytes] | None = None,
    project_dir: Path | None = None,
    operation: str = "transcribe_diarize",
    timeout: int = 900,
    max_attempts: int = 4,
) -> dict[str, Any]:
    """Diarized transcription of one audio file (<=25MB) via Azure OpenAI.

    Uses the classic deployments route: the version-free /openai/v1 audio
    route returns 404 DeploymentNotFound on this resource. `known_speakers`
    maps display names to short (2-10s) mp3 reference clips; the service then
    emits those names as speaker labels instead of A/B/C.
    """
    config = load_azure_openai_config()
    if not config.configured:
        raise RuntimeError("Azure OpenAI endpoint, API key, and API version must be configured")

    audio_bytes = Path(audio_path).read_bytes()
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise ValueError(f"audio file exceeds the 25MB transcription cap: {audio_path}")

    fields: list[tuple[str, str]] = [
        ("response_format", "diarized_json"),
        ("chunking_strategy", "auto"),
    ]
    for name, clip in (known_speakers or {}).items():
        fields.append(("known_speaker_names[]", name))
        fields.append(
            (
                "known_speaker_references[]",
                "data:audio/mpeg;base64," + base64.b64encode(clip).decode("ascii"),
            )
        )
    body, content_type = _encode_multipart(
        fields, files=[("file", Path(audio_path).name, audio_bytes, "audio/mpeg")]
    )

    endpoint = config.endpoint.rstrip("/")
    url = (
        f"{endpoint}/openai/deployments/{deployment}/audio/transcriptions"
        f"?api-version={config.api_version}"
    )
    request = Request(
        url,
        data=body,
        headers={"Content-Type": content_type, "api-key": config.api_key},
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "apim-request-id"
                )
            break
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Azure OpenAI transcription failed ({exc.code}): {detail}")
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == max_attempts:
                raise last_error from exc
            time.sleep(15.0 * attempt)
        except (TimeoutError, URLError, ConnectionError, HTTPException) as exc:
            # stalled reads on long parts are transient — retry like a 5xx
            last_error = RuntimeError(
                f"Azure OpenAI transcription failed ({type(exc).__name__}): {exc}"
            )
            if attempt == max_attempts:
                raise last_error from exc
            time.sleep(15.0 * attempt)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_error or RuntimeError("Azure OpenAI transcription failed")

    result = json.loads(raw)
    usage = result.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    if project_dir is not None:
        append_llm_usage(
            project_dir,
            LlmUsage(
                provider="azure_openai",
                deployment=deployment,
                operation=operation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=0,
                estimated_cost_usd=estimate_llm_cost_usd(
                    provider="azure_openai",
                    deployment=deployment,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
                request_id=request_id,
            ),
        )
    return result


def _encode_multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"tapesplit-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, filename, payload, mime in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(payload)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


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

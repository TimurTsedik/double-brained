import asyncio
import json
from collections.abc import Callable, Mapping
from typing import cast
from urllib.request import ProxyHandler, Request, build_opener

from second_brain.slices.classification.adapters.structured_output import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    parse_candidate_content,
    strict_json_loads,
)
from second_brain.slices.classification.application.contracts import (
    ClassificationDraft,
    ClassificationRequest,
)
from second_brain.slices.classification.domain.entities import (
    ClassificationCandidateDraft,
)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-20b:free",
)
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576

Transport = Callable[[str, bytes, Mapping[str, str], float, int], bytes]


class OpenRouterClassificationError(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class OpenRouterClassificationModel:
    def __init__(
        self,
        api_key: str,
        models: tuple[str, ...] = DEFAULT_MODELS,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key must not be empty")
        if not models or any(not model for model in models):
            raise ValueError("OpenRouter model list must contain non-empty values")
        if timeout_seconds <= 0:
            raise ValueError("OpenRouter timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("OpenRouter response limit must be positive")
        self._api_key = api_key
        self._models = models
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport or _post_json

    async def classify(self, request: ClassificationRequest) -> ClassificationDraft:
        body = json.dumps(
            {
                "models": list(self._models),
                "stream": False,
                "temperature": 0,
                "provider": {"require_parameters": True},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "classification_candidates",
                        "strict": True,
                        "schema": RESPONSE_SCHEMA,
                    },
                },
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": request.source_text},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            raw = await asyncio.to_thread(
                self._transport,
                OPENROUTER_CHAT_URL,
                body,
                headers,
                self._timeout_seconds,
                self._max_response_bytes,
            )
        except Exception:
            raise OpenRouterClassificationError("openrouter_unavailable") from None
        if len(raw) > self._max_response_bytes:
            raise OpenRouterClassificationError("openrouter_response_too_large")
        try:
            model_name, candidates, discarded = _parse_response(raw)
        except Exception:
            raise OpenRouterClassificationError("openrouter_response_invalid") from None
        return ClassificationDraft(
            model_name=model_name,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            candidates=candidates,
            discarded_candidate_count=discarded,
        )


def _post_json(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    request = Request(
        url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
        return cast(bytes, response.read(max_response_bytes + 1))


def _parse_response(
    raw: bytes,
) -> tuple[str, tuple[ClassificationCandidateDraft, ...], int]:
    envelope = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict):
        raise ValueError("response envelope must be an object")
    model_name = envelope.get("model")
    choices = envelope.get("choices")
    if not isinstance(model_name, str) or not model_name:
        raise ValueError("response model is missing")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response choices are missing")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("response choice must be an object")
    message = first_choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("response message content is missing")
    candidates, discarded = parse_candidate_content(message["content"])
    return model_name, candidates, discarded

import asyncio
import json
from collections.abc import Callable
from typing import cast
from urllib.parse import urlsplit
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

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576

Transport = Callable[[str, bytes, float, int], bytes]


class OllamaClassificationError(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class OllamaClassificationModel:
    def __init__(
        self,
        base_url: str,
        model_name: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        if not model_name:
            raise ValueError("classification model name must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("classification timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("classification response limit must be positive")
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport or _post_json

    async def classify(self, request: ClassificationRequest) -> ClassificationDraft:
        body = json.dumps(
            {
                "model": self._model_name,
                "stream": False,
                "think": False,
                "options": {"temperature": 0},
                "format": RESPONSE_SCHEMA,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": request.source_text},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        try:
            raw = await asyncio.to_thread(
                self._transport,
                f"{self._base_url}/api/chat",
                body,
                self._timeout_seconds,
                self._max_response_bytes,
            )
        except Exception:
            raise OllamaClassificationError("ollama_unavailable") from None
        if len(raw) > self._max_response_bytes:
            raise OllamaClassificationError("ollama_response_too_large")
        try:
            candidates, discarded = _parse_response(raw)
        except Exception:
            raise OllamaClassificationError("ollama_response_invalid") from None
        return ClassificationDraft(
            model_name=self._model_name,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            candidates=candidates,
            discarded_candidate_count=discarded,
        )


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama base URL must be plain loopback HTTP")
    return value.rstrip("/")


def _post_json(
    url: str,
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
        return cast(bytes, response.read(max_response_bytes + 1))


def _parse_response(
    raw: bytes,
) -> tuple[tuple[ClassificationCandidateDraft, ...], int]:
    envelope = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict):
        raise ValueError("response envelope must be an object")
    message = envelope.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("response message content is missing")
    return parse_candidate_content(message["content"])

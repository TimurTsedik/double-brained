import json
from collections.abc import Mapping
from urllib.request import ProxyHandler, Request

import pytest

from second_brain.slices.classification.adapters.openrouter import model as model_module
from second_brain.slices.classification.adapters.openrouter.model import (
    DEFAULT_MODELS,
    OPENROUTER_CHAT_URL,
    OpenRouterClassificationError,
    OpenRouterClassificationModel,
)
from second_brain.slices.classification.adapters.structured_output import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
)
from second_brain.slices.classification.application.contracts import (
    ClassificationRequest,
)
from second_brain.slices.classification.domain.entities import (
    CandidateModality,
    CandidateType,
)


class Transport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, bytes, Mapping[str, str], float, int]] = []

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        self.calls.append((url, body, headers, timeout_seconds, max_response_bytes))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def response(
    content: object,
    *,
    model: str = "nvidia/nemotron-3-super-120b-a12b:free",
) -> bytes:
    return json.dumps(
        {
            "id": "safe-generation-id",
            "model": model,
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps(content)}}
            ],
        }
    ).encode()


@pytest.mark.asyncio
async def test_adapter_sends_strict_two_model_request_and_records_actual_model() -> (
    None
):
    source = "Надо проверить классификацию"
    transport = Transport(
        response(
            {
                "items": [
                    {
                        "type": "task",
                        "source_quote": source,
                        "modality": "commitment",
                        "confidence": 0.97,
                    }
                ]
            },
            model="openai/gpt-oss-20b:free",
        )
    )
    adapter = OpenRouterClassificationModel(
        api_key="test-openrouter-secret",
        transport=transport,
    )

    draft = await adapter.classify(ClassificationRequest(source_text=source))

    assert draft.model_name == "openai/gpt-oss-20b:free"
    assert draft.prompt_version == "atomic-extraction-v2"
    assert draft.schema_version == "atomic-candidates-v2"
    assert len(draft.candidates) == 1
    assert draft.candidates[0].candidate_type is CandidateType.TASK
    assert draft.candidates[0].modality is CandidateModality.COMMITMENT

    url, body, headers, timeout, maximum = transport.calls[0]
    payload = json.loads(body)
    assert url == OPENROUTER_CHAT_URL
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert dict(headers) == {
        "Authorization": "Bearer test-openrouter-secret",
        "Content-Type": "application/json",
    }
    assert timeout == 60
    assert maximum == 1_048_576
    assert (
        payload["models"]
        == list(DEFAULT_MODELS)
        == [
            "nvidia/nemotron-3-super-120b-a12b:free",
            "openai/gpt-oss-20b:free",
        ]
    )
    assert "model" not in payload
    assert payload["stream"] is False
    assert payload["temperature"] == 0
    assert payload["provider"] == {"require_parameters": True}
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "classification_candidates",
            "strict": True,
            "schema": RESPONSE_SCHEMA,
        },
    }
    assert payload["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": source},
    ]
    for forbidden in ("user", "telegram_id", "user_space_id", "trace_id", "history"):
        assert forbidden not in payload


def test_default_transport_uses_direct_https_and_preserves_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[object] = []
    requests: list[Request] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == 101
            return b"response"

    class Opener:
        def open(self, request: Request, *, timeout: float) -> Response:
            assert timeout == 2
            requests.append(request)
            return Response()

    def build_opener(*given_handlers: object) -> Opener:
        handlers.extend(given_handlers)
        return Opener()

    monkeypatch.setattr(model_module, "build_opener", build_opener)

    result = model_module._post_json(
        OPENROUTER_CHAT_URL,
        b"{}",
        {
            "Authorization": "Bearer transport-secret",
            "Content-Type": "application/json",
        },
        2,
        100,
    )

    assert result == b"response"
    assert len(handlers) == 1
    assert isinstance(handlers[0], ProxyHandler)
    assert handlers[0].proxies == {}
    assert requests[0].full_url == OPENROUTER_CHAT_URL
    assert requests[0].get_header("Authorization") == "Bearer transport-secret"


@pytest.mark.parametrize(
    ("api_key", "models", "message"),
    [
        ("", DEFAULT_MODELS, "API key"),
        ("secret", (), "model"),
        ("secret", ("",), "model"),
    ],
)
def test_adapter_rejects_invalid_configuration(
    api_key: str,
    models: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        OpenRouterClassificationModel(api_key=api_key, models=models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_result", "safe_code"),
    [
        (RuntimeError("transport leaked private text"), "openrouter_unavailable"),
        (b"not-json-private-text", "openrouter_response_invalid"),
        (b"x" * 101, "openrouter_response_too_large"),
        (
            json.dumps({"model": "fallback", "choices": []}).encode(),
            "openrouter_response_invalid",
        ),
    ],
)
async def test_adapter_exposes_only_fixed_safe_errors(
    provider_result: bytes | Exception,
    safe_code: str,
) -> None:
    adapter = OpenRouterClassificationModel(
        api_key="private-api-key",
        max_response_bytes=100,
        transport=Transport(provider_result),
    )

    with pytest.raises(OpenRouterClassificationError) as caught:
        await adapter.classify(ClassificationRequest(source_text="private source"))

    assert caught.value.safe_error_code == safe_code
    assert repr(caught.value) == f"OpenRouterClassificationError('{safe_code}')"
    assert "private" not in repr(caught.value)

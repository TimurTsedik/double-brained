import json
from collections.abc import Mapping

import pytest

from second_brain.slices.memory.adapters.openrouter import model as model_module
from second_brain.slices.memory.adapters.openrouter.model import (
    OPENROUTER_CHAT_URL,
    REASONING_MODELS,
    OpenRouterReasoningError,
    OpenRouterReasoningModel,
)
from second_brain.slices.memory.application.contracts import (
    LabelledSnippet,
    ReasoningRequest,
)
from second_brain.slices.memory.application.structured_output import (
    REASONING_SYSTEM_PROMPT,
    RESPONSE_SCHEMA,
)
from second_brain.slices.memory.domain.entities import EvidenceLevel

TransportCall = tuple[str, bytes, Mapping[str, str], float, int]


class SequenceTransport:
    """Fake transport: returns one prepared result per model call, in order."""

    def __init__(self, results: list[bytes | Exception]) -> None:
        self._results = list(results)
        self.calls: list[TransportCall] = []

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        self.calls.append((url, body, headers, timeout_seconds, max_response_bytes))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def envelope(content: object, *, model: str) -> bytes:
    return json.dumps(
        {
            "id": "safe-generation-id",
            "model": model,
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps(content)}}
            ],
        }
    ).encode()


def valid(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "evidence_level": "direct",
        "answer": "Краткий обоснованный ответ",
        "source_labels": ["S1"],
    }
    payload.update(overrides)
    return payload


def make_request() -> ReasoningRequest:
    return ReasoningRequest(
        question="Что я решил про деплой?",
        snippets=(
            LabelledSnippet(label="S1", text="снипет один"),
            LabelledSnippet(label="S2", text="снипет два"),
        ),
    )


def test_reasoning_models_are_fixed_in_exact_order() -> None:
    assert REASONING_MODELS == (
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openai/gpt-oss-20b:free",
    )


@pytest.mark.asyncio
async def test_first_valid_model_wins_and_later_models_are_not_called() -> None:
    transport = SequenceTransport(
        [
            envelope(valid(), model="nvidia/nemotron-3-ultra-550b-a55b:free"),
            RuntimeError("second model must not be called"),
            RuntimeError("third model must not be called"),
        ]
    )
    adapter = OpenRouterReasoningModel(api_key="secret-key", transport=transport)

    draft = await adapter.reason(make_request())

    assert len(transport.calls) == 1
    assert draft.model_name == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert draft.prompt_version == "grounded-answer-v1"
    assert draft.schema_version == "grounded-answer-v1"
    assert draft.evidence_level is EvidenceLevel.DIRECT
    assert draft.answer == "Краткий обоснованный ответ"
    assert draft.source_labels == ("S1",)


@pytest.mark.asyncio
async def test_invalid_first_answer_falls_through_to_second_model() -> None:
    transport = SequenceTransport(
        [
            envelope(
                valid(source_labels=["S9"]),  # label not in the request
                model="nvidia/nemotron-3-ultra-550b-a55b:free",
            ),
            envelope(
                valid(evidence_level="reconstructed", source_labels=["S2"]),
                model="nvidia/nemotron-3-super-120b-a12b:free",
            ),
            RuntimeError("third model must not be called"),
        ]
    )
    adapter = OpenRouterReasoningModel(api_key="secret-key", transport=transport)

    draft = await adapter.reason(make_request())

    assert len(transport.calls) == 2
    assert draft.model_name == "nvidia/nemotron-3-super-120b-a12b:free"
    assert draft.evidence_level is EvidenceLevel.RECONSTRUCTED
    assert draft.source_labels == ("S2",)


@pytest.mark.asyncio
async def test_duplicate_labels_reject_first_model_and_fall_through() -> None:
    transport = SequenceTransport(
        [
            envelope(
                valid(source_labels=["S1", "S1"]),  # duplicate label -> malformed
                model="nvidia/nemotron-3-ultra-550b-a55b:free",
            ),
            envelope(
                valid(evidence_level="reconstructed", source_labels=["S2"]),
                model="nvidia/nemotron-3-super-120b-a12b:free",
            ),
            RuntimeError("third model must not be called"),
        ]
    )
    adapter = OpenRouterReasoningModel(api_key="secret-key", transport=transport)

    draft = await adapter.reason(make_request())

    assert len(transport.calls) == 2
    assert draft.model_name == "nvidia/nemotron-3-super-120b-a12b:free"
    assert draft.source_labels == ("S2",)


@pytest.mark.asyncio
async def test_all_three_invalid_raise_safe_reasoning_failure() -> None:
    transport = SequenceTransport(
        [
            RuntimeError("provider timeout leaked text"),  # network failure
            envelope(valid(source_labels=["S9"]), model="ignored"),  # contract reject
            b"{not json private body",  # malformed
        ]
    )
    adapter = OpenRouterReasoningModel(api_key="secret-key", transport=transport)

    with pytest.raises(OpenRouterReasoningError) as caught:
        await adapter.reason(make_request())

    assert len(transport.calls) == 3
    assert caught.value.safe_error_code == "reasoning_unavailable"
    assert repr(caught.value) == "OpenRouterReasoningError('reasoning_unavailable')"


@pytest.mark.asyncio
async def test_each_call_is_bounded_and_oversize_response_is_skipped() -> None:
    transport = SequenceTransport(
        [
            b"x" * 4097,  # exceeds max_response_bytes -> skip
            envelope(valid(), model="nvidia/nemotron-3-super-120b-a12b:free"),
            RuntimeError("third model must not be called"),
        ]
    )
    adapter = OpenRouterReasoningModel(
        api_key="secret-key",
        timeout_seconds=7.5,
        max_response_bytes=4096,
        transport=transport,
    )

    draft = await adapter.reason(make_request())

    assert draft.model_name == "nvidia/nemotron-3-super-120b-a12b:free"
    assert len(transport.calls) == 2
    for _url, _body, _headers, timeout_seconds, max_response_bytes in transport.calls:
        assert timeout_seconds == 7.5
        assert max_response_bytes == 4096


@pytest.mark.asyncio
async def test_answer_carrying_a_credential_falls_through_to_next_model() -> None:
    leaked = valid(answer="ключ sk-abcdefghijklmnopqrstuvwx0123456789")
    transport = SequenceTransport(
        [
            envelope(leaked, model="nvidia/nemotron-3-ultra-550b-a55b:free"),
            envelope(valid(), model="nvidia/nemotron-3-super-120b-a12b:free"),
            RuntimeError("third model must not be called"),
        ]
    )
    adapter = OpenRouterReasoningModel(api_key="secret-key", transport=transport)

    draft = await adapter.reason(make_request())

    assert len(transport.calls) == 2
    assert draft.model_name == "nvidia/nemotron-3-super-120b-a12b:free"
    assert draft.answer == "Краткий обоснованный ответ"


@pytest.mark.asyncio
async def test_error_leaks_no_question_answer_or_provider_body() -> None:
    transport = SequenceTransport(
        [
            RuntimeError("provider body: секретный вопрос про деплой"),
            RuntimeError("provider body: секретный вопрос про деплой"),
            RuntimeError("provider body: секретный вопрос про деплой"),
        ]
    )
    adapter = OpenRouterReasoningModel(api_key="private-api-key", transport=transport)

    with pytest.raises(OpenRouterReasoningError) as caught:
        await adapter.reason(make_request())

    text = f"{caught.value!r} {caught.value}"
    assert "деплой" not in text
    assert "provider body" not in text
    assert "private-api-key" not in text


@pytest.mark.asyncio
async def test_payload_carries_only_question_and_labelled_snippets() -> None:
    transport = SequenceTransport(
        [envelope(valid(), model="nvidia/nemotron-3-ultra-550b-a55b:free")]
    )
    adapter = OpenRouterReasoningModel(api_key="test-secret", transport=transport)

    await adapter.reason(make_request())

    url, body, headers, timeout_seconds, max_response_bytes = transport.calls[0]
    payload = json.loads(body)
    assert url == OPENROUTER_CHAT_URL
    assert dict(headers) == {
        "Authorization": "Bearer test-secret",
        "Content-Type": "application/json",
    }
    assert timeout_seconds == 60
    assert max_response_bytes == 1_048_576
    assert payload["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert "models" not in payload
    assert payload["stream"] is False
    assert payload["temperature"] == 0
    assert payload["provider"] == {"require_parameters": True}
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "grounded_answer",
            "strict": True,
            "schema": RESPONSE_SCHEMA,
        },
    }
    assert payload["messages"][0] == {
        "role": "system",
        "content": REASONING_SYSTEM_PROMPT,
    }
    user_message = payload["messages"][1]
    assert user_message["role"] == "user"
    sent = json.loads(user_message["content"])
    assert set(sent) == {"question", "snippets"}
    assert sent["question"] == "Что я решил про деплой?"
    assert sent["snippets"] == [
        {"label": "S1", "text": "снипет один"},
        {"label": "S2", "text": "снипет два"},
    ]
    for snippet in sent["snippets"]:
        assert set(snippet) == {"label", "text"}
    serialized = body.decode("utf-8")
    for forbidden in (
        "telegram_id",
        "user_space_id",
        "trace_id",
        "record_id",
        "history",
        "current_project_id",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("api_key", "models", "message"),
    [
        ("", REASONING_MODELS, "API key"),
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
        OpenRouterReasoningModel(api_key=api_key, models=models)


def test_default_transport_uses_direct_https_and_preserves_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.request import ProxyHandler, Request

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

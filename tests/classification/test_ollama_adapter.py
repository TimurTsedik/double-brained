import json
from urllib.request import ProxyHandler

import pytest

from second_brain.slices.classification.adapters.ollama import model as model_module
from second_brain.slices.classification.adapters.ollama.model import (
    OllamaClassificationError,
    OllamaClassificationModel,
)
from second_brain.slices.classification.application.contracts import (
    ClassificationRequest,
)
from second_brain.slices.classification.domain.entities import (
    CandidateModality,
    CandidateType,
)


class Transport:
    def __init__(self, response: bytes | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, bytes, float, int]] = []

    def __call__(
        self,
        url: str,
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        self.calls.append((url, body, timeout_seconds, max_response_bytes))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response(content: object) -> bytes:
    return json.dumps(
        {"message": {"content": json.dumps(content, ensure_ascii=False)}},
        ensure_ascii=False,
    ).encode()


def test_default_transport_disables_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[object] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == 101
            return b"response"

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            assert timeout == 2
            return Response()

    def build_opener(*given_handlers: object) -> Opener:
        handlers.extend(given_handlers)
        return Opener()

    monkeypatch.setattr(model_module, "build_opener", build_opener, raising=False)

    result = model_module._post_json("http://127.0.0.1:11434/api/chat", b"{}", 2, 100)

    assert result == b"response"
    assert len(handlers) == 1
    assert isinstance(handlers[0], ProxyHandler)
    assert handlers[0].proxies == {}


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:11434",
        "http://ollama.example:11434",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434/untrusted-path",
    ],
)
def test_adapter_accepts_only_plain_loopback_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaClassificationModel(base_url, "qwen3:4b")


@pytest.mark.asyncio
async def test_adapter_sends_fixed_schema_and_parses_valid_siblings() -> None:
    transport = Transport(
        response(
            {
                "items": [
                    {
                        "type": "task",
                        "source_quote": "Надо позвонить Сергею",
                        "modality": "commitment",
                        "confidence": 0.95,
                    },
                    {
                        "type": "question",
                        "source_quote": "Использовать Qdrant?",
                        "modality": "question",
                        "confidence": 0.9,
                    },
                    {
                        "type": "invented-type",
                        "source_quote": "invalid sibling",
                        "modality": "observation",
                        "confidence": 1,
                    },
                ]
            }
        )
    )
    model = OllamaClassificationModel(
        "http://127.0.0.1:11434",
        "qwen3:4b",
        transport=transport,
    )

    draft = await model.classify(
        ClassificationRequest(source_text="Надо позвонить Сергею. Использовать Qdrant?")
    )

    assert draft.model_name == "qwen3:4b"
    assert draft.prompt_version == "atomic-extraction-v3"
    assert draft.schema_version == "atomic-candidates-v2"
    assert draft.discarded_candidate_count == 1
    assert [item.candidate_type for item in draft.candidates] == [
        CandidateType.TASK,
        CandidateType.QUESTION,
    ]
    assert [item.modality for item in draft.candidates] == [
        CandidateModality.COMMITMENT,
        CandidateModality.QUESTION,
    ]

    url, body, timeout, maximum = transport.calls[0]
    payload = json.loads(body)
    assert url == "http://127.0.0.1:11434/api/chat"
    assert timeout == 60
    assert maximum == 1_048_576
    assert payload["model"] == "qwen3:4b"
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["options"] == {"temperature": 0}
    assert payload["format"]["additionalProperties"] is False
    assert payload["format"]["properties"]["items"]["maxItems"] == 8
    item_schema = payload["format"]["properties"]["items"]["items"]
    type_modality_pairs = {
        branch["properties"]["type"]["const"]: tuple(
            branch["properties"]["modality"]["enum"]
        )
        for branch in item_schema["oneOf"]
    }
    assert type_modality_pairs == {
        "note": ("observation", "completed_action"),
        "task": ("commitment",),
        "idea": ("suggestion", "hypothesis"),
        "decision": ("decision",),
        "question": ("question",),
    }
    assert [message["role"] for message in payload["messages"]] == [
        "system",
        "user",
    ]
    assert payload["messages"][1]["content"] == (
        "Надо позвонить Сергею. Использовать Qdrant?"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_result", "safe_code"),
    [
        (RuntimeError("provider leaked private text"), "ollama_unavailable"),
        (b"not-json-private-text", "ollama_response_invalid"),
        (b"x" * 1_048_577, "ollama_response_too_large"),
    ],
)
async def test_adapter_exposes_only_fixed_safe_errors(
    provider_result: bytes | Exception,
    safe_code: str,
) -> None:
    model = OllamaClassificationModel(
        "http://localhost:11434",
        "qwen3:4b",
        transport=Transport(provider_result),
    )

    with pytest.raises(OllamaClassificationError) as caught:
        await model.classify(ClassificationRequest(source_text="private source"))

    assert caught.value.safe_error_code == safe_code
    assert repr(caught.value) == f"OllamaClassificationError('{safe_code}')"
    assert "private" not in repr(caught.value)


@pytest.mark.asyncio
async def test_boolean_confidence_and_unexpected_fields_are_discarded() -> None:
    model = OllamaClassificationModel(
        "http://[::1]:11434",
        "qwen3:4b",
        transport=Transport(
            response(
                {
                    "items": [
                        {
                            "type": "task",
                            "source_quote": "one",
                            "modality": "commitment",
                            "confidence": True,
                        },
                        {
                            "type": "task",
                            "source_quote": "two",
                            "modality": "commitment",
                            "confidence": 0.95,
                            "unexpected": "value",
                        },
                    ]
                }
            )
        ),
    )

    draft = await model.classify(ClassificationRequest(source_text="one two"))

    assert draft.candidates == ()
    assert draft.discarded_candidate_count == 2

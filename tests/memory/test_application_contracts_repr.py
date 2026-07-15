from second_brain.slices.memory.application.contracts import (
    DeliveryPayload,
    LabelledSnippet,
    ReasoningDraft,
    ReasoningRequest,
)
from second_brain.slices.memory.domain.entities import EvidenceLevel


def test_reasoning_request_repr_hides_question_and_text() -> None:
    request = ReasoningRequest(
        question="secret question about pricing",
        snippets=(LabelledSnippet(label="S1", text="secret snippet body"),),
    )

    text = repr(request)

    assert "secret question about pricing" not in text
    assert "secret snippet body" not in text


def test_reasoning_draft_repr_hides_answer() -> None:
    draft = ReasoningDraft(
        model_name="nvidia/nemotron",
        prompt_version="grounded-answer-v1",
        schema_version="grounded-answer-v1",
        evidence_level=EvidenceLevel.DIRECT,
        answer="secret answer text",
        source_labels=("S1",),
    )

    text = repr(draft)

    assert "secret answer text" not in text
    assert "S1" in text


def test_delivery_payload_repr_hides_text_and_trace() -> None:
    success = DeliveryPayload.success("secret answer text")
    failure = DeliveryPayload.failure("reasoning_unavailable", "trace-secret-123")

    assert "secret answer text" not in repr(success)
    assert "trace-secret-123" not in repr(failure)
    assert "reasoning_unavailable" in repr(failure)

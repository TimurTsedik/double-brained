from datetime import UTC, datetime
from uuid import uuid4

import pytest

from second_brain.slices.memory.application.answer_question import AnswerMemoryQuestion
from second_brain.slices.memory.application.contracts import (
    ReasoningDraft,
    ReasoningRequest,
)
from second_brain.slices.memory.application.structured_output import (
    ReasoningContractError,
)
from second_brain.slices.memory.domain.entities import (
    EvidenceLevel,
    EvidenceSnippet,
    MemoryRecordKind,
)

_CREATED = datetime(2026, 7, 15, tzinfo=UTC)


class RecordingReasoner:
    def __init__(
        self,
        draft: ReasoningDraft | None = None,
        error: Exception | None = None,
    ) -> None:
        self._draft = draft
        self._error = error
        self.calls = 0
        self.requests: list[ReasoningRequest] = []

    async def reason(self, request: ReasoningRequest) -> ReasoningDraft:
        self.calls += 1
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._draft is not None
        return self._draft


def snippet(
    text: str,
    kind: MemoryRecordKind = MemoryRecordKind.NOTE,
    label: str = "S1",
) -> EvidenceSnippet:
    return EvidenceSnippet(
        label=label,
        record_kind=kind,
        record_id=uuid4(),
        source_capture_event_id=uuid4(),
        created_at=_CREATED,
        text=text,
    )


def draft(level: EvidenceLevel, labels: tuple[str, ...]) -> ReasoningDraft:
    return ReasoningDraft(
        model_name="nvidia/nemotron-3-ultra-550b-a55b:free",
        prompt_version="grounded-answer-v1",
        schema_version="grounded-answer-v1",
        evidence_level=level,
        answer="Сводный вывод",
        source_labels=labels,
    )


@pytest.mark.asyncio
async def test_empty_snapshot_returns_insufficient_without_calling_provider() -> None:
    reasoner = RecordingReasoner(draft(EvidenceLevel.DIRECT, ("S1",)))

    answer = await AnswerMemoryQuestion(reasoner).execute("вопрос", ())

    assert reasoner.calls == 0
    assert answer.evidence_level is EvidenceLevel.INSUFFICIENT
    assert answer.sources == ()
    assert answer.model_name is None


@pytest.mark.asyncio
async def test_provenance_is_built_from_snapshot_map_not_model_strings() -> None:
    first = snippet("alpha", label="S1")
    second = snippet("beta", MemoryRecordKind.TASK, label="S2")
    reasoner = RecordingReasoner(draft(EvidenceLevel.RECONSTRUCTED, ("S2",)))

    answer = await AnswerMemoryQuestion(reasoner).execute("вопрос", (first, second))

    assert reasoner.calls == 1
    request = reasoner.requests[0]
    assert [item.label for item in request.snippets] == ["S1", "S2"]
    assert [item.text for item in request.snippets] == ["alpha", "beta"]
    assert answer.evidence_level is EvidenceLevel.RECONSTRUCTED
    assert len(answer.sources) == 1
    source = answer.sources[0]
    assert source.label == "S2"
    assert source.record_kind is MemoryRecordKind.TASK
    assert source.record_id == second.record_id
    assert source.source_capture_event_id == second.source_capture_event_id
    assert source.created_at == second.created_at
    assert answer.model_name == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert answer.prompt_version == "grounded-answer-v1"
    assert answer.schema_version == "grounded-answer-v1"


@pytest.mark.asyncio
async def test_unknown_label_from_model_raises_safe_contract_error() -> None:
    reasoner = RecordingReasoner(draft(EvidenceLevel.DIRECT, ("S9",)))

    with pytest.raises(ReasoningContractError) as excinfo:
        await AnswerMemoryQuestion(reasoner).execute("вопрос", (snippet("alpha"),))

    assert excinfo.value.safe_error_code == "reasoning_contract_violation"
    assert "alpha" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_provider_error_propagates_as_safe_code() -> None:
    class Boom(RuntimeError):
        def __init__(self) -> None:
            self.safe_error_code = "reasoning_unavailable"
            super().__init__("reasoning_unavailable")

    reasoner = RecordingReasoner(error=Boom())

    with pytest.raises(Boom) as excinfo:
        await AnswerMemoryQuestion(reasoner).execute("вопрос", (snippet("alpha"),))

    assert excinfo.value.safe_error_code == "reasoning_unavailable"

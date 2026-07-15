from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
    UpdateTransaction,
)
from second_brain.slices.memory.domain.entities import EvidenceLevel


@dataclass(frozen=True, slots=True)
class LabelledSnippet:
    # Exactly what reaches the model for one snippet: an opaque label and text.
    label: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReasoningRequest:
    question: str = field(repr=False)
    snippets: tuple[LabelledSnippet, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReasoningDraft:
    model_name: str
    prompt_version: str
    schema_version: str
    evidence_level: EvidenceLevel
    answer: str = field(repr=False)
    source_labels: tuple[str, ...]


class ReasoningModel(Protocol):
    async def reason(self, request: ReasoningRequest) -> ReasoningDraft: ...


@dataclass(frozen=True, slots=True)
class DeliveryPayload:
    # Success carries the ready answer text; failure carries a safe code plus a
    # trace reference. The delivery port never receives a bare MemoryAnswer, so
    # the FAILED-reasoning path stays deliverable.
    text: str | None = field(default=None, repr=False)
    safe_error_code: str | None = None
    trace_id: str | None = field(default=None, repr=False)

    @classmethod
    def success(cls, text: str) -> "DeliveryPayload":
        return cls(text=text)

    @classmethod
    def failure(cls, safe_error_code: str, trace_id: str) -> "DeliveryPayload":
        return cls(safe_error_code=safe_error_code, trace_id=trace_id)


class AnswerDeliveryPort(Protocol):
    async def deliver(
        self, payload: DeliveryPayload, recipient_context: TelegramRecipient
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SetAwaitingMemoryCommand:
    # access_context has no repr=False on its own fields, so guard it here.
    access_context: AccessContext = field(repr=False)
    updated_at: datetime
    trace_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ConsumeMemoryQuestionCommand:
    access_context: AccessContext = field(repr=False)
    bot_id: int = field(repr=False)
    telegram_update_id: int = field(repr=False)
    question: str = field(repr=False)
    created_at: datetime
    trace_id: str = field(repr=False)
    current_project_id: UUID | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MemoryAskResult:
    # question_required=True keeps the one-shot mode armed for the next text.
    question_required: bool


class MemoryQuestionPort(Protocol):
    async def set_awaiting(
        self,
        command: SetAwaitingMemoryCommand,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def cancel(
        self,
        access_context: AccessContext,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def consume_question(
        self,
        command: ConsumeMemoryQuestionCommand,
        transaction: UpdateTransaction,
    ) -> MemoryAskResult | None: ...

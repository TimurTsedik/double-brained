from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.domain.entities import (
    Decision,
    Idea,
    Note,
    Question,
)
from second_brain.slices.knowledge.domain.entities import (
    KnowledgeRecordKind as KnowledgeRecordKind,
)

KnowledgeRecord = Note | Idea | Decision | Question


@dataclass(frozen=True)
class CreateNoteCommand:
    access_context: AccessContext
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CreateIdeaCommand:
    access_context: AccessContext
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CreateDecisionCommand:
    access_context: AccessContext
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CreateQuestionCommand:
    access_context: AccessContext
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class UpdateKnowledgeTextCommand:
    """Заменить текст записи (правка, S3): text + updated_at, больше ничего.

    created_at/trace_id/источник записи неизменяемы — история происхождения
    остаётся историей создания; идентификатор правки живёт в receipt-журнале.
    """

    access_context: AccessContext
    record_kind: KnowledgeRecordKind
    record_id: UUID = field(repr=False)
    text: str = field(repr=False)
    updated_at: datetime


class KnowledgeCapturePort(Protocol):
    """Published knowledge-capture boundary for another application slice."""

    async def create_note(self, command: CreateNoteCommand) -> Note: ...

    async def create_idea(self, command: CreateIdeaCommand) -> Idea: ...

    async def create_decision(self, command: CreateDecisionCommand) -> Decision: ...

    async def create_question(self, command: CreateQuestionCommand) -> Question: ...

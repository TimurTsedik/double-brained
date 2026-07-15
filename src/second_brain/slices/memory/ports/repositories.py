from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.application.contracts import SetAwaitingMemoryCommand
from second_brain.slices.memory.domain.entities import (
    EvidenceSnippet,
    MemoryAnswer,
    MemoryAnswerStep,
    MemoryQuestion,
    MemoryReasoningState,
    MemoryRunClaim,
)


@dataclass(frozen=True)
class CreateMemoryQuestionCommand:
    access_context: AccessContext
    bot_id: int = field(repr=False)
    telegram_update_id: int = field(repr=False)
    question_text: str = field(repr=False)
    current_project_id: UUID | None = field(repr=False)
    created_at: datetime
    trace_id: str = field(repr=False)


@dataclass(frozen=True)
class SnapshotEvidenceCommand:
    access_context: AccessContext
    run_id: UUID = field(repr=False)
    snippets: tuple[EvidenceSnippet, ...] = field(repr=False)


@dataclass(frozen=True)
class SaveMemoryAnswerCommand:
    access_context: AccessContext
    run_id: UUID = field(repr=False)
    answer: MemoryAnswer = field(repr=False)
    created_at: datetime
    trace_id: str = field(repr=False)


@dataclass(frozen=True)
class SucceedMemoryStepCommand:
    access_context: AccessContext
    step_id: UUID = field(repr=False)
    completed_at: datetime


@dataclass(frozen=True)
class FailMemoryStepCommand:
    access_context: AccessContext
    step_id: UUID = field(repr=False)
    failed_at: datetime
    safe_error_code: str


class MemoryStore(Protocol):
    async def set_awaiting(self, command: SetAwaitingMemoryCommand) -> None: ...

    async def cancel(self, access_context: AccessContext) -> None: ...

    async def lock_pending(self, access_context: AccessContext) -> bool: ...

    async def create_question(
        self, command: CreateMemoryQuestionCommand
    ) -> MemoryQuestion: ...

    async def claim_due_run(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
    ) -> MemoryRunClaim | None: ...

    async def read_run_question(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryQuestion | None: ...

    async def snapshot_evidence(self, command: SnapshotEvidenceCommand) -> None: ...

    async def read_evidence_snapshot(
        self, access_context: AccessContext, run_id: UUID
    ) -> tuple[EvidenceSnippet, ...]: ...

    async def save_answer(self, command: SaveMemoryAnswerCommand) -> None: ...

    async def read_answer(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryAnswer | None: ...

    async def read_reasoning_state(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryReasoningState | None: ...

    async def succeed_step(
        self, command: SucceedMemoryStepCommand
    ) -> MemoryAnswerStep: ...

    async def fail_step(self, command: FailMemoryStepCommand) -> MemoryAnswerStep: ...

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID


class MemoryRunStatus(IntEnum):
    # Own status model for the memory slice. Numeric values and order mirror
    # ProcessingStepStatus so the established machine order (overall = min over
    # steps) can be reused, but the processing enum is never imported here.
    # A pin test asserts the numeric equivalence to catch drift.
    FAILED = 0
    NEEDS_REVIEW = 1
    RUNNING = 2
    PENDING = 3
    SUCCEEDED = 4
    SKIPPED = 5


class MemoryStepType(StrEnum):
    RETRIEVAL = "retrieval"
    REASONING = "reasoning"
    DELIVERY = "delivery"


class EvidenceLevel(StrEnum):
    DIRECT = "direct"
    RECONSTRUCTED = "reconstructed"
    HYPOTHESIS = "hypothesis"
    INSUFFICIENT = "insufficient"


class MemoryRecordKind(StrEnum):
    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


@dataclass(frozen=True)
class MemoryQuestion:
    id: UUID = field(repr=False)
    user_space_id: UUID = field(repr=False)
    bot_id: int = field(repr=False)
    telegram_update_id: int = field(repr=False)
    question_text: str = field(repr=False)
    current_project_id: UUID | None = field(repr=False)
    created_at: datetime
    trace_id: str = field(repr=False)


@dataclass(frozen=True)
class AnswerSource:
    label: str
    record_kind: MemoryRecordKind
    record_id: UUID = field(repr=False)
    source_capture_event_id: UUID = field(repr=False)
    created_at: datetime


@dataclass(frozen=True)
class MemoryAnswer:
    evidence_level: EvidenceLevel
    answer_text: str = field(repr=False)
    sources: tuple[AnswerSource, ...] = field(repr=False)
    model_name: str | None
    prompt_version: str | None
    schema_version: str | None


@dataclass(frozen=True)
class EvidenceSnippet:
    label: str
    record_kind: MemoryRecordKind
    record_id: UUID = field(repr=False)
    source_capture_event_id: UUID = field(repr=False)
    created_at: datetime
    text: str = field(repr=False)


@dataclass(frozen=True)
class MemoryAnswerStep:
    id: UUID = field(repr=False)
    step_type: MemoryStepType
    status: MemoryRunStatus
    attempt_count: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    safe_error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class MemoryAnswerRun:
    id: UUID = field(repr=False)
    user_space_id: UUID = field(repr=False)
    question_id: UUID = field(repr=False)
    steps: tuple[MemoryAnswerStep, ...]
    created_at: datetime
    trace_id: str = field(repr=False)

    @property
    def overall_status(self) -> MemoryRunStatus:
        return overall_status(tuple(step.status for step in self.steps))


@dataclass(frozen=True)
class MemoryRunClaim:
    step_id: UUID = field(repr=False)
    run_id: UUID = field(repr=False)
    question_id: UUID = field(repr=False)
    step_type: MemoryStepType
    attempt_count: int
    lease_expires_at: datetime
    trace_id: str = field(repr=False)


@dataclass(frozen=True)
class MemoryReasoningState:
    status: MemoryRunStatus
    has_answer: bool


def overall_status(
    statuses: tuple[MemoryRunStatus, ...],
) -> MemoryRunStatus:
    return min(statuses, default=MemoryRunStatus.PENDING)

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID


class ProcessingStepStatus(IntEnum):
    FAILED = 0
    NEEDS_REVIEW = 1
    RUNNING = 2
    PENDING = 3
    SUCCEEDED = 4
    SKIPPED = 5


class ProcessingStepType(StrEnum):
    AUDIO_DOWNLOAD = "audio_download"
    TRANSCRIPTION = "transcription"


class TranscriptionOutputType(StrEnum):
    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


class ProcessingNoticeKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class ProcessingNoticeStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"


@dataclass(frozen=True)
class ProcessingStep:
    id: UUID
    step_type: ProcessingStepType
    status: ProcessingStepStatus
    attempt_count: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    safe_error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class ProcessingRun:
    id: UUID
    user_space_id: UUID
    capture_event_id: UUID
    output_type: TranscriptionOutputType
    version: int
    steps: tuple[ProcessingStep, ...]
    trace_id: str

    @property
    def overall_status(self) -> ProcessingStepStatus:
        return overall_status(tuple(step.status for step in self.steps))


@dataclass(frozen=True)
class ProcessingStepClaim:
    step_id: UUID
    run_id: UUID
    capture_event_id: UUID
    step_type: ProcessingStepType
    output_type: TranscriptionOutputType
    attempt_count: int
    lease_expires_at: datetime
    trace_id: str


@dataclass(frozen=True)
class TranscriptWord:
    start: float
    end: float
    text: str = field(repr=False)


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str = field(repr=False)
    words: tuple[TranscriptWord, ...] = field(repr=False)


def overall_status(
    statuses: tuple[ProcessingStepStatus, ...],
) -> ProcessingStepStatus:
    return min(statuses, default=ProcessingStepStatus.PENDING)

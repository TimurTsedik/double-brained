from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class TaskStatus(StrEnum):
    INBOX = "inbox"
    COMPLETED = "completed"


class PendingCaptureType(StrEnum):
    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


@dataclass(frozen=True)
class Task:
    id: UUID
    user_space_id: UUID
    title: str = field(repr=False)
    description: str | None
    status: TaskStatus
    source_capture_event_id: UUID
    created_at: datetime
    updated_at: datetime
    trace_id: str

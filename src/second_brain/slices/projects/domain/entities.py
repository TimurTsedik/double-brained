from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ProjectContentKind(StrEnum):
    CAPTURE_EVENT = "capture_event"
    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


@dataclass(frozen=True)
class Project:
    id: UUID
    user_space_id: UUID
    name: str = field(repr=False)
    created_at: datetime
    updated_at: datetime
    trace_id: str

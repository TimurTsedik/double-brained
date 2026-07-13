from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID


class SearchRecordType(StrEnum):
    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


class MatchQuality(IntEnum):
    SUBSTRING = 0
    FULL_TEXT = 1


@dataclass(frozen=True)
class SearchRecord:
    id: UUID
    record_type: SearchRecordType
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    task_completed: bool | None
    match_quality: MatchQuality

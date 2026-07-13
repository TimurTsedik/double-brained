from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Note:
    id: UUID
    user_space_id: UUID
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class Idea:
    id: UUID
    user_space_id: UUID
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class Decision:
    id: UUID
    user_space_id: UUID
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class Question:
    id: UUID
    user_space_id: UUID
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    updated_at: datetime
    trace_id: str

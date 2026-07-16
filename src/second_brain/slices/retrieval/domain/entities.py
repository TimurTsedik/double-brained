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
class IndexedChunk:
    chunk_number: int
    content_sha256: str
    text: str = field(repr=False)
    embedding: tuple[float, ...] = field(repr=False)


@dataclass(frozen=True)
class IndexingTarget:
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    capture_event_id: UUID = field(repr=False)


@dataclass(frozen=True)
class SemanticMatch:
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    source_capture_event_id: UUID = field(repr=False)
    chunk_number: int
    text: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True)
class EvidenceChunk:
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    source_capture_event_id: UUID = field(repr=False)
    chunk_number: int | None  # None = full-record FTS hit (pseudo-chunk)
    text: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True)
class EvidenceBundle:
    chunks: tuple[EvidenceChunk, ...]
    current_project_id: UUID | None = field(repr=False)


@dataclass(frozen=True)
class SearchRecord:
    id: UUID
    record_type: SearchRecordType
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    task_completed: bool | None
    match_quality: MatchQuality


@dataclass(frozen=True)
class RecordView:
    """Каноническая запись для показа целиком (и как элемент «похожего»)."""

    id: UUID
    record_type: SearchRecordType
    text: str = field(repr=False)
    created_at: datetime
    task_completed: bool | None

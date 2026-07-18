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
    # Запись рождена из подписи к фото — списки помечают её 📷 (спека §2.2).
    has_image_source: bool = False


@dataclass(frozen=True)
class RecordView:
    """Каноническая запись для показа целиком (и как элемент «похожего»)."""

    id: UUID
    record_type: SearchRecordType
    text: str = field(repr=False)
    created_at: datetime
    task_completed: bool | None
    # У записи есть изображение-источник (capture_events.source_kind='image') —
    # показ добавляет пометку «📷 …». Для списков остаётся дефолтным False.
    has_image_source: bool = False


class DigestPeriod(StrEnum):
    """Календарный период сводки (закрытый список — часть callback-контракта)."""

    WEEK = "week"
    MONTH = "month"
    HALF_YEAR = "half_year"
    YEAR = "year"


@dataclass(frozen=True)
class DigestCounters:
    """Счётчики записей периода по типам; задачи — с числом выполненных."""

    notes: int
    tasks: int
    tasks_completed: int
    ideas: int
    decisions: int
    questions: int

    @property
    def total(self) -> int:
        return self.notes + self.tasks + self.ideas + self.decisions + self.questions

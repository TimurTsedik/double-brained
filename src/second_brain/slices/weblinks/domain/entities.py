"""Домен ссылок захвата: sidecar-пары «слово → адрес» и титулы страниц.

Текст пользователя неприкосновенен: ссылки живут РЯДОМ с записью
(record_urls), а <title> страницы — производные метаданные (page_titles),
подтягиваемые фоновым воркером. Ничего не вшивается в текст записи.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class WeblinkRecordKind(StrEnum):
    """Вид типизированной записи, к которой привязаны ссылки."""

    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


class PageTitleStatus(StrEnum):
    """Статус фонового фетча <title>: pending → fetched | failed."""

    PENDING = "pending"
    FETCHED = "fetched"
    FAILED = "failed"


@dataclass(frozen=True)
class RecordUrl:
    """Упорядоченная ссылка записи: label для text_link, для голого URL —
    сам URL. Текст записи выше — дословный, эта пара — sidecar."""

    id: UUID
    user_space_id: UUID
    record_kind: WeblinkRecordKind
    record_id: UUID = field(repr=False)
    position: int
    label: str = field(repr=False)
    url: str = field(repr=False)
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class PageTitle:
    """Титул страницы по нормализованному URL — метаданные РЯДОМ с записью."""

    id: UUID
    user_space_id: UUID
    original_url: str = field(repr=False)
    normalized_url: str = field(repr=False)
    title: str | None = field(repr=False)
    status: PageTitleStatus
    attempt_count: int
    next_attempt_at: datetime | None
    fetched_at: datetime | None
    created_at: datetime
    updated_at: datetime
    trace_id: str

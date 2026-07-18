"""Опубликованные контракты weblinks: команды записи sidecar-ссылок и
чтение «label/url/title» для показа записи целиком."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.weblinks.domain.entities import (
    PageTitleStatus as PageTitleStatus,
)
from second_brain.slices.weblinks.domain.entities import (
    WeblinkRecordKind as WeblinkRecordKind,
)


@dataclass(frozen=True)
class RecordUrlEntry:
    """Пара «слово → адрес» в порядке появления в тексте (label и url — PII)."""

    label: str = field(repr=False)
    url: str = field(repr=False)


@dataclass(frozen=True)
class SaveRecordLinksCommand:
    """Записать ссылки фактически созданной записи + идемпотентно поставить
    их URL в очередь титулов (page_titles, status=pending)."""

    access_context: AccessContext = field(repr=False)
    record_kind: WeblinkRecordKind
    record_id: UUID = field(repr=False)
    entries: tuple[RecordUrlEntry, ...] = field(repr=False)
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class RecordLinkView:
    """Ссылка записи для показа: title подтянут, если страница уже fetched."""

    label: str = field(repr=False)
    url: str = field(repr=False)
    title: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ClaimedPageTitle:
    """Захваченная воркером строка очереди титулов (одна на транзакцию)."""

    page_title_id: UUID
    original_url: str = field(repr=False)
    attempt_count: int
    trace_id: str


class RecordLinksPort(Protocol):
    """Ссылки записи (label/url/title) внутри update-транзакции показа."""

    async def links_for_record(
        self,
        access_context: AccessContext,
        record_kind: WeblinkRecordKind,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> tuple[RecordLinkView, ...]: ...

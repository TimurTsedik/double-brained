from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.retrieval.domain.entities import (
    DigestCounters as DigestCounters,
)
from second_brain.slices.retrieval.domain.entities import DigestPeriod as DigestPeriod
from second_brain.slices.retrieval.domain.entities import (
    EvidenceBundle,
    IndexedChunk,
)
from second_brain.slices.retrieval.domain.entities import RecordView as RecordView
from second_brain.slices.retrieval.domain.entities import SearchRecord as SearchRecord
from second_brain.slices.retrieval.domain.entities import (
    SearchRecordType as SearchRecordType,
)
from second_brain.slices.weblinks.application.contracts import RecordLinkView

EMBEDDING_MODEL_NAME = "intfloat/multilingual-e5-base"
EMBEDDING_DIMENSIONS = 768
INDEX_VERSION = 1


@dataclass(frozen=True)
class IndexingSource:
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    text: str = field(repr=False)
    # created_at of the source record itself, not of any processing step:
    # the semantic projection must carry the record's date.
    created_at: datetime


@dataclass(frozen=True)
class IndexingOutcome:
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    chunks: tuple[IndexedChunk, ...] = field(repr=False)
    # Copied from IndexingSource.created_at: the record's own date.
    created_at: datetime


@dataclass(frozen=True)
class RegisterIndexingTargetCommand:
    access_context: AccessContext = field(repr=False)
    processing_run_id: UUID = field(repr=False)
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class StoreSemanticChunksCommand:
    access_context: AccessContext = field(repr=False)
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    source_capture_event_id: UUID = field(repr=False)
    chunks: tuple[IndexedChunk, ...] = field(repr=False)
    embedding_model: str
    index_version: int
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class SetAwaitingSearchCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ConsumeSearchQueryCommand:
    access_context: AccessContext
    query: str = field(repr=False)


@dataclass(frozen=True)
class SearchPanelResult:
    items: tuple[SearchRecord, ...]
    query_required: bool


@dataclass(frozen=True)
class RetrieveMemoryCommand:
    # repr=False on access_context too: AccessContext is another slice's plain
    # dataclass whose user_id/user_space_id would otherwise leak via our repr.
    access_context: AccessContext = field(repr=False)
    question: str = field(repr=False)
    # Pass-through metadata for presentation, never a retrieval filter.
    current_project_id: UUID | None = field(default=None, repr=False)


class MemoryRetrievalPort(Protocol):
    """Public retrieval contract consumed by the future memory slice."""

    async def retrieve(self, command: RetrieveMemoryCommand) -> EvidenceBundle: ...


@dataclass(frozen=True)
class RecordImageSource:
    """Изображение-источник записи для показа (спека §2.2).

    file_id — только fast path (bot-локален, не вечное хранилище); источник
    истины — скачанные байты хранилища: local_path None, пока download-шаг
    воркера не сохранил оригинал.
    """

    telegram_file_id: str = field(repr=False)
    local_path: str | None = field(repr=False)


@dataclass(frozen=True)
class RecordViewResult:
    # Полный текст записи и «похожее» — transient-payload показа: текст записей
    # не должен просочиться в repr/логи.
    record: RecordView = field(repr=False)
    related: tuple[RecordView, ...] = field(repr=False)
    # Sidecar-ссылки записи (label/url/title) для блока «🔗 Ссылки:» под
    # дословным текстом; тоже пользовательское содержимое — вне repr/логов.
    links: tuple[RecordLinkView, ...] = field(default=(), repr=False)
    # Изображение-источник (file_id — PII): показ шлёт фото отдельным
    # сообщением после текста записи.
    image: RecordImageSource | None = field(default=None, repr=False)


class RecordViewPort(Protocol):
    """Показ записи целиком + «похожее по смыслу» внутри update-транзакции."""

    async def read_record_full(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> RecordView | None: ...

    async def related_records(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> tuple[RecordView, ...]: ...

    async def image_source_for_record(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> RecordImageSource | None: ...


@dataclass(frozen=True)
class DigestPage:
    """Страница сводки одного снимка as_of — transient-payload показа.

    Тексты записей не должны просочиться в repr/логи. `period_start` и `as_of`
    уже в поясе пространства (транспорт только форматирует); `total` — все
    записи снимка, по нему транспорт решает судьбу кнопки «Ещё».
    """

    period: DigestPeriod
    period_start: datetime
    as_of: datetime
    offset: int
    total: int
    counters: DigestCounters
    items: tuple[RecordView, ...] = field(repr=False)


class DigestPort(Protocol):
    """Сводка за календарный период внутри update-транзакции."""

    async def read_digest_page(
        self,
        access_context: AccessContext,
        period: DigestPeriod,
        offset: int,
        as_of: datetime,
        transaction: UpdateTransaction,
    ) -> DigestPage: ...


class ExactSearchPort(Protocol):
    async def set_awaiting(
        self,
        command: SetAwaitingSearchCommand,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def cancel(
        self,
        access_context: AccessContext,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def consume_query(
        self,
        command: ConsumeSearchQueryCommand,
        transaction: UpdateTransaction,
    ) -> SearchPanelResult | None: ...

from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.bootstrap.task_capture_in_transaction import (
    PostgresSpaceTimezoneReader,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresRecordViewReader,
)
from second_brain.slices.retrieval.application.contracts import (
    RecordImageSource,
    RecordView,
    RecordViewPort,
)
from second_brain.slices.retrieval.application.record_view import ShowRecord
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresWeblinkWriter,
)
from second_brain.slices.weblinks.application.contracts import (
    RecordLinksPort,
    RecordLinkView,
    WeblinkRecordKind,
)


class RecordViewInTransaction(RecordViewPort, RecordLinksPort):
    """Bootstrap-композиция показа записи целиком внутри update-транзакции."""

    def __init__(self, image_storage_root: str | None = None) -> None:
        # Корень хранилища оригиналов фото: storage_key → локальный путь для
        # фоллбека байтами. None (тесты без фото) — байтового фоллбека нет.
        self._image_storage_root = image_storage_root

    async def read_record_full(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> RecordView | None:
        session = _active_session(transaction)
        record = await ShowRecord(PostgresRecordViewReader(session)).read_record_full(
            access_context, record_type, record_id
        )
        if record is None:
            return None
        # Дата заголовка показывается в часовом поясе пространства (как ack
        # напоминаний): конвертируем здесь, транспорт только форматирует.
        timezone = await PostgresSpaceTimezoneReader(session).resolve_timezone(
            access_context
        )
        return replace(
            record, created_at=record.created_at.astimezone(ZoneInfo(timezone))
        )

    async def related_records(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> tuple[RecordView, ...]:
        session = _active_session(transaction)
        return await ShowRecord(PostgresRecordViewReader(session)).related_records(
            access_context, record_type, record_id
        )

    async def image_source_for_record(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> RecordImageSource | None:
        # file_id + локальный путь к скачанному оригиналу (если download-шаг
        # уже отработал): fast path и фоллбек байтами для показа фото.
        session = _active_session(transaction)
        attachment = await PostgresRecordViewReader(session).image_attachment(
            access_context, record_type, record_id
        )
        if attachment is None:
            return None
        telegram_file_id, storage_key = attachment
        local_path = None
        if storage_key is not None and self._image_storage_root is not None:
            local_path = contained_image_path(self._image_storage_root, storage_key)
        return RecordImageSource(
            telegram_file_id=telegram_file_id, local_path=local_path
        )

    async def links_for_record(
        self,
        access_context: AccessContext,
        record_kind: WeblinkRecordKind,
        record_id: UUID,
        transaction: UpdateTransaction,
    ) -> tuple[RecordLinkView, ...]:
        # Sidecar-ссылки записи (label/url/title) — той же update-транзакцией
        # и под тем же RLS, что и сама запись.
        session = _active_session(transaction)
        return await PostgresWeblinkWriter(session).links_for_record(
            access_context, record_kind, record_id
        )


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("record view requires the PostgreSQL update transaction")
    return transaction.active_session


def contained_image_path(root: str, storage_key: str) -> str | None:
    """Локальный путь оригинала СТРОГО внутри корня хранилища, иначе None.

    storage_key приходит из БД: абсолютный путь или «../» в испорченной строке
    заставили бы show-full отправить произвольный локальный файл — такой ключ
    отвергается (фото просто не шлётся, текстовая пометка остаётся).
    """
    resolved_root = Path(root).expanduser().resolve()
    key = PurePosixPath(storage_key)
    if key.is_absolute() or ".." in key.parts:
        return None
    candidate = resolved_root.joinpath(*key.parts).resolve()
    if not candidate.is_relative_to(resolved_root):
        return None
    return str(candidate)

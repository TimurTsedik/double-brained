from dataclasses import replace
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

from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.domain.entities import RecordView, SearchRecordType
from second_brain.slices.retrieval.ports.repositories import RecordViewStore

RELATED_RECORDS_LIMIT = 3


class ShowRecord:
    """Показ записи целиком и подбор «похожего по смыслу» по своему индексу."""

    def __init__(self, store: RecordViewStore) -> None:
        self._store = store

    async def read_record_full(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
    ) -> RecordView | None:
        # Чтение строго по тройке (тип, uuid, пространство вызывающего): тип из
        # callback'а не доверенный, id-таблицы независимы — по uuid без типа нельзя.
        return await self._store.read_record(access_context, record_type, record_id)

    async def related_records(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        limit: int = RELATED_RECORDS_LIMIT,
    ) -> tuple[RecordView, ...]:
        # Кандидаты уже детерминированно отранжированы (минимальная дистанция по
        # ВСЕМ своим чанкам) и дедуплицированы до записей; здесь — join обратно в
        # каноническую типовую таблицу под тем же контекстом: осиротевший чанк
        # без канонической строки просто выпадает, без объяснений.
        related: list[RecordView] = []
        for kind, candidate_id in await self._store.related_candidates(
            access_context, record_type, record_id, limit
        ):
            record = await self._store.read_record(access_context, kind, candidate_id)
            if record is not None:
                related.append(record)
        return tuple(related)

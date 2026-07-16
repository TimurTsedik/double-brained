from datetime import datetime
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
    PostgresDigestReader,
)
from second_brain.slices.retrieval.application.contracts import (
    DigestPage,
    DigestPeriod,
    DigestPort,
)
from second_brain.slices.retrieval.application.digest import BuildDigest


class DigestInTransaction(DigestPort):
    """Bootstrap-композиция сводки за период внутри update-транзакции.

    Начало периода считается в часовом поясе пространства (как ack напоминаний):
    пояс резолвится здесь, календарь и чтения — в retrieval-слое.
    """

    async def read_digest_page(
        self,
        access_context: AccessContext,
        period: DigestPeriod,
        offset: int,
        as_of: datetime,
        transaction: UpdateTransaction,
    ) -> DigestPage:
        session = _active_session(transaction)
        timezone = await PostgresSpaceTimezoneReader(session).resolve_timezone(
            access_context
        )
        return await BuildDigest(PostgresDigestReader(session)).read_page(
            access_context, period, offset, as_of, ZoneInfo(timezone)
        )


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("digest requires the PostgreSQL update transaction")
    return transaction.active_session

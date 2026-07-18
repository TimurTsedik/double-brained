"""Postgres-INBOX webhook-апдейтов: идемпотентная постановка и строгий порядок.

Таблица telegram_update_inbox — технический путь ДО резолва пользователя (как
telegram_update_receipts): без user_space_id и без RLS. Claim отдаёт ТОЛЬКО
головную строку бота (min update_id среди pending) и только когда она созрела:
незрелая голова (бэкофф) блокирует хвост — строгий порядок важнее throughput.
failed-голова хвост НЕ блокирует.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.inbox import (
    PostgresTelegramInboxQueue,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramUpdateInbox,
)
from second_brain.slices.identity.domain.entities import TelegramInboxStatus
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
BOT_ID = 700
MAX_ATTEMPTS = 3
BACKOFF = timedelta(seconds=60)
TRACE_ID = "5" * 32


@pytest_asyncio.fixture(autouse=True)
async def reset_inbox_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


def payload(update_id: int) -> dict[str, object]:
    return {"update_id": update_id, "message": {"text": "private-text"}}


async def enqueue(engine: AsyncEngine, update_id: int, *, bot_id: int = BOT_ID) -> bool:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        return await PostgresTelegramInboxQueue(session).enqueue(
            bot_id=bot_id,
            update_id=update_id,
            payload=payload(update_id),
            received_at=NOW + timedelta(seconds=update_id),
            trace_id=TRACE_ID,
        )


async def claim(
    engine: AsyncEngine, now: datetime, *, bot_id: int = BOT_ID
) -> object | None:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        return await PostgresTelegramInboxQueue(session).claim_head(
            now, bot_id=bot_id, max_attempts=MAX_ATTEMPTS, retry_backoff=BACKOFF
        )


async def mark_done(engine: AsyncEngine, inbox_id: object, now: datetime) -> None:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await PostgresTelegramInboxQueue(session).mark_done(inbox_id, now)


async def record_failure(engine: AsyncEngine, inbox_id: object, now: datetime) -> None:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await PostgresTelegramInboxQueue(session).record_failure(
            inbox_id, max_attempts=MAX_ATTEMPTS
        )


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_per_bot_and_update(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    assert await enqueue(engine, 11) is True
    assert await enqueue(engine, 11) is False
    assert await enqueue(engine, 11, bot_id=BOT_ID + 1) is True

    rows = (await session.scalars(select(TelegramUpdateInbox))).all()
    assert sorted((row.bot_id, row.update_id) for row in rows) == [
        (BOT_ID, 11),
        (BOT_ID + 1, 11),
    ]
    assert {row.status for row in rows} == {TelegramInboxStatus.PENDING}
    assert {row.attempt_count for row in rows} == {0}


@pytest.mark.asyncio
async def test_payload_stays_out_of_row_repr(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await enqueue(engine, 12)

    row = (await session.scalars(select(TelegramUpdateInbox))).one()
    assert "private-text" not in repr(row)


@pytest.mark.asyncio
async def test_claim_returns_rows_strictly_in_update_id_order(
    engine: AsyncEngine,
) -> None:
    for update_id in (33, 31, 32):
        await enqueue(engine, update_id)

    claimed_order = []
    for step in range(3):
        claimed = await claim(engine, NOW + timedelta(minutes=step))
        assert claimed is not None
        claimed_order.append(claimed.update_id)
        await mark_done(engine, claimed.id, NOW + timedelta(minutes=step))

    assert claimed_order == [31, 32, 33]
    assert await claim(engine, NOW + timedelta(minutes=10)) is None


@pytest.mark.asyncio
async def test_unripe_head_blocks_the_tail(engine: AsyncEngine) -> None:
    await enqueue(engine, 41)
    await enqueue(engine, 42)

    claimed = await claim(engine, NOW)
    assert claimed is not None
    assert claimed.update_id == 41
    await record_failure(engine, claimed.id, NOW)

    # Голова pending и ждёт бэкофф — хвост (42) не выдаётся.
    assert await claim(engine, NOW + timedelta(seconds=1)) is None

    ripe = await claim(engine, NOW + BACKOFF + timedelta(seconds=1))
    assert ripe is not None
    assert ripe.update_id == 41
    assert ripe.attempt_count == 2


@pytest.mark.asyncio
async def test_failed_head_does_not_block_the_tail(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await enqueue(engine, 51)
    await enqueue(engine, 52)

    moment = NOW
    for _attempt in range(MAX_ATTEMPTS):
        claimed = await claim(engine, moment)
        assert claimed is not None
        assert claimed.update_id == 51
        await record_failure(engine, claimed.id, moment)
        moment = moment + timedelta(hours=1)

    tail = await claim(engine, moment)
    assert tail is not None
    assert tail.update_id == 52

    head_status = await session.scalar(
        select(TelegramUpdateInbox.status).where(TelegramUpdateInbox.update_id == 51)
    )
    assert head_status is TelegramInboxStatus.FAILED


@pytest.mark.asyncio
async def test_claim_fails_over_budget_head_left_by_a_crash(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    # Крах между claim'ом и итогом: попытки уже выбраны, строка осталась
    # pending. Следующий claim добивает её в failed и отдаёт хвост.
    await enqueue(engine, 61)
    await enqueue(engine, 62)
    await session.execute(
        text(
            "UPDATE telegram_update_inbox SET attempt_count = :attempts "
            "WHERE update_id = 61"
        ),
        {"attempts": MAX_ATTEMPTS},
    )
    await session.commit()

    claimed = await claim(engine, NOW + timedelta(minutes=5))
    assert claimed is not None
    assert claimed.update_id == 62

    head_status = await session.scalar(
        select(TelegramUpdateInbox.status).where(TelegramUpdateInbox.update_id == 61)
    )
    assert head_status is TelegramInboxStatus.FAILED


@pytest.mark.asyncio
async def test_read_status_reports_depth_and_head_age(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as session:
        empty = await PostgresTelegramInboxQueue(session).read_status(
            NOW, bot_id=BOT_ID
        )
    assert (empty.pending_count, empty.failed_count) == (0, 0)
    assert empty.head_age_seconds is None

    await enqueue(engine, 71)  # received_at = NOW + 71s
    await enqueue(engine, 72)
    moment = NOW + timedelta(minutes=2)
    for attempt in range(MAX_ATTEMPTS):
        claimed = await claim(engine, moment)
        assert claimed is not None
        assert claimed.update_id == 71
        await record_failure(engine, claimed.id, moment)
        moment = moment + timedelta(hours=1 + attempt)

    async with factory() as session:
        status = await PostgresTelegramInboxQueue(session).read_status(
            NOW + timedelta(seconds=172), bot_id=BOT_ID
        )
    assert (status.pending_count, status.failed_count) == (1, 1)
    # Голова pending — апдейт 72 (received_at = NOW+72с) → возраст 100с.
    assert status.head_age_seconds == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_claim_returns_only_rows_of_the_requested_bot(
    engine: AsyncEngine,
) -> None:
    # Чужой pending (другой бот) не выдаётся и не мешает пустой очереди бота.
    await enqueue(engine, 81, bot_id=BOT_ID + 1)
    assert await claim(engine, NOW + timedelta(minutes=1)) is None

    await enqueue(engine, 82)
    claimed = await claim(engine, NOW + timedelta(minutes=1))
    assert claimed is not None
    assert (claimed.bot_id, claimed.update_id) == (BOT_ID, 82)


@pytest.mark.asyncio
async def test_read_status_ignores_done_rows_and_foreign_bots(
    engine: AsyncEngine,
) -> None:
    # Done-история (самая старая строка) и pending чужого бота не должны
    # попадать ни в счётчики, ни в возраст головы.
    await enqueue(engine, 91)  # received_at = NOW+91с → уйдёт в done
    await enqueue(engine, 92)  # received_at = NOW+92с → голова pending
    await enqueue(engine, 93, bot_id=BOT_ID + 1)

    claimed = await claim(engine, NOW + timedelta(minutes=2))
    assert claimed is not None
    assert claimed.update_id == 91
    await mark_done(engine, claimed.id, NOW + timedelta(minutes=2))

    factory = create_session_factory(engine)
    async with factory() as session:
        status = await PostgresTelegramInboxQueue(session).read_status(
            NOW + timedelta(seconds=192), bot_id=BOT_ID
        )
    assert (status.pending_count, status.failed_count) == (1, 0)
    # Голова pending — апдейт 92 (received_at = NOW+92с) → возраст 100с.
    assert status.head_age_seconds == pytest.approx(100.0)

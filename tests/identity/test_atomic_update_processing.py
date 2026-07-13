import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    EnrollmentAttempt,
    TelegramUpdateReceipt,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresPollerLock,
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    reset_prototype_schema,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
PEPPER = b"task5-atomic-pepper"
HIGH_TELEGRAM_ID = 2**31 + 7


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_task5_schema(engine: AsyncEngine) -> None:
    await reset_prototype_schema(engine, confirm=True)


def raw_start(update_id: int, token: str = "transient-start-token") -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=f"/start {token}",
    )


def raw_plain_start(update_id: int) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text="/start",
    )


def test_telegram_dto_keeps_raw_text_transient_and_unparsed() -> None:
    update = raw_start(1)

    assert update.text == "/start transient-start-token"
    assert "transient-start-token" not in repr(update)
    assert not hasattr(update, "start_token")


@pytest.mark.asyncio
async def test_real_postgres_duplicate_processing_parses_once_after_receipt_lock(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        PEPPER,
        "key-1",
    )

    first, second = await asyncio.gather(
        processor.process(raw_start(2)), processor.process(raw_start(2))
    )

    receipt_count = await session.scalar(
        select(func.count()).select_from(TelegramUpdateReceipt)
    )
    attempt_count = await session.scalar(
        select(func.count()).select_from(EnrollmentAttempt)
    )
    attempt = await session.scalar(select(EnrollmentAttempt))
    assert first.kind is AcknowledgementKind.ENROLLMENT_REJECTED
    assert second.kind is AcknowledgementKind.ENROLLMENT_REJECTED
    assert first.trace_id == second.trace_id
    assert first.span_id != second.span_id
    assert receipt_count == 1
    assert attempt_count == 1
    assert attempt is not None
    assert attempt.bot_id == 1
    assert attempt.pepper_key_id == "key-1"
    assert attempt.result_code == AcknowledgementKind.ENROLLMENT_REJECTED.value
    assert attempt.trace_id == first.trace_id
    receipt = await session.scalar(select(TelegramUpdateReceipt))
    assert receipt is not None
    assert receipt.created_at == NOW


@pytest.mark.asyncio
async def test_real_postgres_rate_limit_admits_at_most_five_concurrent_attempts(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        PEPPER,
        "key-1",
    )

    results = await asyncio.gather(
        *(processor.process(raw_start(update_id)) for update_id in range(1, 7))
    )

    attempts = list((await session.scalars(select(EnrollmentAttempt))).all())
    assert all(
        result.kind is AcknowledgementKind.ENROLLMENT_REJECTED for result in results
    )
    assert len(attempts) == 6
    assert [attempt.result_code for attempt in attempts].count("rate_limited") == 1
    assert [attempt.result_code for attempt in attempts].count(
        AcknowledgementKind.ENROLLMENT_REJECTED.value
    ) == 5


@pytest.mark.asyncio
async def test_unknown_private_plain_start_records_a_safe_rejection_attempt(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        PEPPER,
        "key-1",
    )

    result = await processor.process(raw_plain_start(7))

    attempt = await session.scalar(select(EnrollmentAttempt))
    assert result.kind is AcknowledgementKind.ENROLLMENT_REJECTED
    assert attempt is not None
    assert attempt.result_code == "missing_token"
    assert attempt.pepper_key_id == "key-1"
    assert attempt.trace_id == result.trace_id


@pytest.mark.asyncio
async def test_quota_exhausted_plain_start_keeps_the_rate_limited_audit_code(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        PEPPER,
        "key-1",
    )

    for update_id in range(1, 6):
        await processor.process(raw_start(update_id))
    result = await processor.process(raw_plain_start(6))

    attempt = await session.scalar(
        select(EnrollmentAttempt).where(EnrollmentAttempt.trace_id == result.trace_id)
    )
    assert result.kind is AcknowledgementKind.ENROLLMENT_REJECTED
    assert attempt is not None
    assert attempt.result_code == "rate_limited"


@pytest.mark.asyncio
async def test_real_postgres_high_telegram_ids_use_safe_update_and_poller_locks(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        PEPPER,
        "key-1",
    )
    update = TelegramUpdate(
        bot_id=HIGH_TELEGRAM_ID,
        update_id=HIGH_TELEGRAM_ID + 1,
        is_private=False,
        telegram_user_id=None,
        text=None,
    )
    first_lock = PostgresPollerLock(engine)
    second_lock = PostgresPollerLock(engine)
    try:
        result = await processor.process(update)

        receipt = await session.scalar(select(TelegramUpdateReceipt))
        assert result.kind is AcknowledgementKind.IGNORED
        assert receipt is not None
        assert receipt.bot_id == HIGH_TELEGRAM_ID
        assert await first_lock.acquire(HIGH_TELEGRAM_ID) is True
        assert await second_lock.acquire(HIGH_TELEGRAM_ID) is False
    finally:
        await first_lock.close()
        await second_lock.close()

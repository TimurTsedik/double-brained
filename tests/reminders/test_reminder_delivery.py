from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.reminder_delivery import ReminderDeliveryStep
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.adapters.persistence.repository import (
    PostgresReminderRepository,
)
from second_brain.slices.reminders.application.contracts import CreateReminderCommand
from second_brain.slices.reminders.domain.entities import Reminder, ReminderStatus
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresTaskRepository,
)
from second_brain.slices.tasks.application.contracts import CreateTaskCommand
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


class SpyReminderDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []

    async def deliver(self, text: str, recipient: TelegramRecipient) -> int:
        self.sent.append((text, recipient.telegram_user_id))
        # Telegram message_id доставленного сообщения (как у aiogram Message).
        return 777_000 + len(self.sent)


class FixedWorkerIdentity:
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        return (ACCESS,)

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        return TelegramRecipient(telegram_user_id=42)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        return Locale.RU


@pytest_asyncio.fixture(autouse=True)
async def reset_reminder_delivery_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="member",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=ACCESS.user_space_id,
                owner_user_id=ACCESS.user_id,
                timezone="Asia/Jerusalem",
                language="ru",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


async def seed_reminder(
    engine: AsyncEngine, *, title: str, remind_at: datetime, update_id: int
) -> Reminder:
    session_factory = create_session_factory(engine)
    source = await PostgresCaptureEventRepository(session_factory).create(
        CaptureTextCommand(
            access_context=ACCESS,
            bot_id=100,
            telegram_update_id=update_id,
            telegram_message_id=update_id + 1_000,
            raw_text=title,
            received_at=NOW,
            trace_id="1" * 32,
        )
    )
    task = await PostgresTaskRepository(session_factory).create(
        CreateTaskCommand(
            access_context=ACCESS,
            title=title,
            source_capture_event_id=source.id,
            created_at=NOW,
            trace_id="1" * 32,
        )
    )
    return await PostgresReminderRepository(session_factory).create_reminder(
        CreateReminderCommand(
            access_context=ACCESS,
            remind_at=remind_at,
            text=title,
            source_task_id=task.id,
            created_at=NOW,
            trace_id="1" * 32,
        )
    )


def delivery_step(
    engine: AsyncEngine, spy: SpyReminderDelivery
) -> ReminderDeliveryStep:
    return ReminderDeliveryStep(
        create_session_factory(engine), spy, FixedWorkerIdentity()
    )


async def reminder_statuses(schema_engine: AsyncEngine) -> list[ReminderStatus]:
    async with create_session_factory(schema_engine)() as session:
        models = await session.scalars(
            select(ReminderModel).order_by(ReminderModel.remind_at)
        )
        return [model.status for model in models]


@pytest.mark.asyncio
async def test_due_reminder_is_sent_once_and_marked_sent(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW - timedelta(minutes=1),
        update_id=300,
    )
    spy = SpyReminderDelivery()
    step = delivery_step(engine, spy)

    worked = await step.process_once(ACCESS, NOW)

    assert worked is True
    assert spy.sent == [("⏰ Напоминание: Позвонить в банк", 42)]
    assert await reminder_statuses(schema_engine) == [ReminderStatus.SENT]


@pytest.mark.asyncio
async def test_running_the_step_again_does_not_resend(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW - timedelta(minutes=1),
        update_id=310,
    )
    spy = SpyReminderDelivery()
    step = delivery_step(engine, spy)

    first = await step.process_once(ACCESS, NOW)
    second = await step.process_once(ACCESS, NOW)

    assert first is True
    assert second is False
    assert len(spy.sent) == 1
    assert await reminder_statuses(schema_engine) == [ReminderStatus.SENT]


@pytest.mark.asyncio
async def test_not_yet_due_reminder_is_not_sent(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW + timedelta(hours=1),
        update_id=320,
    )
    spy = SpyReminderDelivery()
    step = delivery_step(engine, spy)

    worked = await step.process_once(ACCESS, NOW)

    assert worked is False
    assert spy.sent == []
    assert await reminder_statuses(schema_engine) == [ReminderStatus.PENDING]


@pytest.mark.asyncio
async def test_two_due_reminders_are_both_delivered_one_claimed_unit_each(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_reminder(
        engine,
        title="Первое",
        remind_at=NOW - timedelta(minutes=2),
        update_id=330,
    )
    await seed_reminder(
        engine,
        title="Второе",
        remind_at=NOW - timedelta(minutes=1),
        update_id=331,
    )
    spy = SpyReminderDelivery()
    step = delivery_step(engine, spy)

    worked = await step.process_once(ACCESS, NOW)

    assert worked is True
    # Каждое — своя claimed-единица; порядок по remind_at.
    assert spy.sent == [
        ("⏰ Напоминание: Первое", 42),
        ("⏰ Напоминание: Второе", 42),
    ]
    assert await reminder_statuses(schema_engine) == [
        ReminderStatus.SENT,
        ReminderStatus.SENT,
    ]


class FailingDelivery:
    async def deliver(self, text: str, recipient: TelegramRecipient) -> None:
        raise RuntimeError("telegram is down")


async def read_single_reminder(schema_engine: AsyncEngine) -> ReminderModel:
    async with create_session_factory(schema_engine)() as session:
        model = await session.scalar(select(ReminderModel))
    assert model is not None
    return model


@pytest.mark.asyncio
async def test_successful_delivery_stores_the_telegram_message_id(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Доказательство доставки: «sent» в базе подтверждается телеграмным
    # message_id реально отправленного сообщения, а не только нашим статусом.
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW - timedelta(minutes=1),
        update_id=370,
    )
    spy = SpyReminderDelivery()

    worked = await delivery_step(engine, spy).process_once(ACCESS, NOW)

    assert worked is True
    reminder = await read_single_reminder(schema_engine)
    assert reminder.status is ReminderStatus.SENT
    assert reminder.telegram_message_id == 777_001


@pytest.mark.asyncio
async def test_failed_send_stores_no_telegram_message_id(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW - timedelta(minutes=1),
        update_id=371,
    )
    failing_step = ReminderDeliveryStep(
        create_session_factory(engine), FailingDelivery(), FixedWorkerIdentity()
    )

    await failing_step.process_once(ACCESS, NOW)

    reminder = await read_single_reminder(schema_engine)
    assert reminder.status is ReminderStatus.PENDING
    assert reminder.telegram_message_id is None


@pytest.mark.asyncio
async def test_send_failure_records_one_attempt_and_backs_off_a_minute(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Сбой отправки НЕ теряет claimed-единицу и НЕ долбит Telegram каждый тик:
    # строка остаётся pending, попытка учтена, следующая — через минуту.
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW - timedelta(minutes=1),
        update_id=340,
    )
    failing_step = ReminderDeliveryStep(
        create_session_factory(engine), FailingDelivery(), FixedWorkerIdentity()
    )

    worked = await failing_step.process_once(ACCESS, NOW)
    immediate_retry = await failing_step.process_once(ACCESS, NOW)

    assert worked is True
    assert immediate_retry is False  # бэкофф: сразу следующий тик НЕ переclaim'ит
    reminder = await read_single_reminder(schema_engine)
    assert reminder.status is ReminderStatus.PENDING
    assert reminder.send_attempts == 1
    assert reminder.next_attempt_at == NOW + timedelta(seconds=60)

    spy = SpyReminderDelivery()
    recovered = await delivery_step(engine, spy).process_once(
        ACCESS, NOW + timedelta(seconds=61)
    )

    assert recovered is True
    assert len(spy.sent) == 1
    assert await reminder_statuses(schema_engine) == [ReminderStatus.SENT]


@pytest.mark.asyncio
async def test_fifth_consecutive_failure_marks_the_reminder_failed_forever(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_reminder(
        engine,
        title="Позвонить в банк",
        remind_at=NOW - timedelta(minutes=1),
        update_id=350,
    )
    failing_step = ReminderDeliveryStep(
        create_session_factory(engine), FailingDelivery(), FixedWorkerIdentity()
    )

    tick = NOW
    for _ in range(5):
        assert await failing_step.process_once(ACCESS, tick) is True
        tick += timedelta(minutes=10)  # заведомо позже любого бэкоффа

    reminder = await read_single_reminder(schema_engine)
    assert reminder.status is ReminderStatus.FAILED
    assert reminder.send_attempts == 5

    # failed больше НИКОГДА не claim'ится — даже исправной доставкой и много позже.
    spy = SpyReminderDelivery()
    worked = await delivery_step(engine, spy).process_once(
        ACCESS, tick + timedelta(days=1)
    )
    assert worked is False
    assert spy.sent == []


@pytest.mark.asyncio
async def test_backing_off_reminder_does_not_starve_a_later_one(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Раннее напоминание падает (например, текст неотправляем) — позднее того же
    # пространства всё равно уходит в ЭТОТ же тик: сбой не блокирует очередь.
    await seed_reminder(
        engine,
        title="Первое",
        remind_at=NOW - timedelta(minutes=2),
        update_id=360,
    )
    await seed_reminder(
        engine,
        title="Второе",
        remind_at=NOW - timedelta(minutes=1),
        update_id=361,
    )

    class FailsOnlyFirst:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def deliver(self, text: str, recipient: TelegramRecipient) -> None:
            if "Первое" in text:
                raise RuntimeError("telegram rejected this one")
            self.sent.append(text)

    port = FailsOnlyFirst()
    step = ReminderDeliveryStep(
        create_session_factory(engine), port, FixedWorkerIdentity()
    )

    worked = await step.process_once(ACCESS, NOW)

    assert worked is True
    assert port.sent == ["⏰ Напоминание: Второе"]
    assert await reminder_statuses(schema_engine) == [
        ReminderStatus.PENDING,  # «Первое» — попытка учтена, ждёт бэкофф
        ReminderStatus.SENT,
    ]

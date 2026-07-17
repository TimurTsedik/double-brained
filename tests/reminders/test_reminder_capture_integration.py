from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.domain.entities import ReminderStatus
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from tests.identity.conftest import IsolatedDatabase

# 15:00 в Иерусалиме (UTC+3 летом).
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
JERUSALEM = ZoneInfo("Asia/Jerusalem")
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_reminder_capture_schema(
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
        await connection.execute(
            insert(TelegramIdentity).values(
                id=UUID("00000000-0000-0000-0000-000000000021"),
                telegram_user_id=42,
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=value,
        telegram_message_id=update_id + 1_000,
    )


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    transaction_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        transaction_port,
        transaction_port,
        transaction_port,
        reminder_ack_port=transaction_port,
    )


@pytest.mark.asyncio
async def test_task_with_clock_time_creates_reminder_and_announces_space_local_time(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)

    await app.process(callback(100, "task:await_text"))
    result = await app.process(text_update(101, "Позвонить в банк завтра в 10:00"))

    assert result.kind is AcknowledgementKind.CAPTURED
    # Ack называет момент в tz пространства: завтра 10:00 по Иерусалиму (+03:00).
    assert result.reminder_when == datetime(2026, 7, 14, 10, 0, tzinfo=JERUSALEM)
    assert result.reminder_when is not None
    assert result.reminder_when.utcoffset() == timedelta(hours=3)
    assert (result.reminder_when.hour, result.reminder_when.minute) == (10, 0)
    async with create_session_factory(schema_engine)() as session:
        task = await session.scalar(select(TaskModel))
        reminder = await session.scalar(select(ReminderModel))
    assert task is not None
    assert reminder is not None
    # remind_at хранится в UTC: 10:00 Иерусалима = 07:00 UTC.
    assert reminder.remind_at == datetime(2026, 7, 14, 7, 0, tzinfo=UTC)
    assert reminder.status is ReminderStatus.PENDING
    assert reminder.source_task_id == task.id
    assert reminder.user_space_id == task.user_space_id
    assert reminder.text == task.title
    assert reminder.trace_id == task.trace_id


@pytest.mark.asyncio
async def test_default_text_with_time_becomes_one_reminder_task_without_a_button(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Кнопка НЕ нажата: текст с явным будущим временем сам становится задачей с
    # напоминанием — ОДНА запись (не «заметка + задача»).
    app = processor(engine)

    result = await app.process(text_update(140, "Позвонить Ави завтра в 10:00"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert result.reminder_when == datetime(2026, 7, 14, 10, 0, tzinfo=JERUSALEM)
    async with create_session_factory(schema_engine)() as session:
        task = await session.scalar(select(TaskModel))
        reminder = await session.scalar(select(ReminderModel))
        notes = await session.scalar(select(func.count()).select_from(NoteModel))
    assert task is not None
    assert reminder is not None
    assert reminder.source_task_id == task.id
    assert reminder.remind_at == datetime(2026, 7, 14, 7, 0, tzinfo=UTC)
    # Никакой параллельной заметки — дубля нет.
    assert notes == 0


@pytest.mark.asyncio
async def test_explicit_note_button_keeps_note_even_with_a_time(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # «Кнопка главнее»: явно нажал «📝 Заметка» → запись останется заметкой даже
    # с явным временем в тексте. Авто-напоминание — только когда кнопку НЕ жали.
    app = processor(engine)

    await app.process(callback(160, "capture:note"))
    result = await app.process(text_update(161, "позвонить Ави завтра в 10:00"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert result.reminder_when is None
    async with create_session_factory(schema_engine)() as session:
        notes = await session.scalar(select(func.count()).select_from(NoteModel))
        tasks = await session.scalar(select(func.count()).select_from(TaskModel))
        reminders = await session.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert (notes, tasks, reminders) == (1, 0, 0)


@pytest.mark.asyncio
async def test_default_text_without_time_stays_a_note_without_a_button(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Кнопка НЕ нажата, времени в тексте нет → обычная заметка, без задачи и
    # напоминания.
    app = processor(engine)

    result = await app.process(text_update(150, "Красивый закат над морем"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert result.reminder_when is None
    async with create_session_factory(schema_engine)() as session:
        notes = await session.scalar(select(func.count()).select_from(NoteModel))
        tasks = await session.scalar(select(func.count()).select_from(TaskModel))
        reminders = await session.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert notes == 1
    assert tasks == 0
    assert reminders == 0


@pytest.mark.asyncio
async def test_task_without_time_creates_no_reminder_and_keeps_plain_ack(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)

    await app.process(callback(110, "task:await_text"))
    result = await app.process(text_update(111, "Купить молоко"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert result.reminder_when is None
    async with create_session_factory(schema_engine)() as session:
        tasks = await session.scalar(select(func.count()).select_from(TaskModel))
        reminders = await session.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert tasks == 1
    assert reminders == 0


@pytest.mark.asyncio
async def test_task_with_explicit_past_date_creates_no_reminder(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)

    await app.process(callback(120, "task:await_text"))
    result = await app.process(text_update(121, "Отчитаться вчера в 9"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert result.reminder_when is None
    async with create_session_factory(schema_engine)() as session:
        reminders = await session.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert reminders == 0


@pytest.mark.asyncio
async def test_completing_the_task_cancels_its_pending_reminder(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(callback(130, "task:await_text"))
    await app.process(text_update(131, "Позвонить в банк завтра в 10:00"))
    async with create_session_factory(schema_engine)() as session:
        task = await session.scalar(select(TaskModel))
    assert task is not None

    result = await app.process(callback(132, f"tasks:complete:{task.id}"))

    assert result.kind is AcknowledgementKind.TASK_COMPLETED
    async with create_session_factory(schema_engine)() as session:
        reminder = await session.scalar(select(ReminderModel))
    assert reminder is not None
    assert reminder.status is ReminderStatus.CANCELLED

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import initialize_schema, reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.adapters.persistence.repository import (
    PostgresReminderRepository,
    PostgresReminderWriter,
)
from second_brain.slices.reminders.application.contracts import (
    CancelReminderForTaskCommand,
    CreateReminderCommand,
)
from second_brain.slices.reminders.domain.entities import Reminder, ReminderStatus
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresTaskRepository,
)
from second_brain.slices.tasks.application.contracts import CreateTaskCommand
from second_brain.slices.tasks.domain.entities import Task
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_reminder_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                {
                    "id": ACCESS_A.user_id,
                    # Пространство A = admin, B = member: admin НЕ суперпользователь,
                    # RLS изолирует по user_space_id в обе стороны.
                    "role": "admin",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": ACCESS_A.user_space_id,
                    "owner_user_id": ACCESS_A.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_space_id,
                    "owner_user_id": ACCESS_B.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )


async def seed_task(
    engine: AsyncEngine, access_context: AccessContext, *, update_id: int
) -> Task:
    session_factory = create_session_factory(engine)
    source = await PostgresCaptureEventRepository(session_factory).create(
        CaptureTextCommand(
            access_context=access_context,
            bot_id=100,
            telegram_update_id=update_id,
            telegram_message_id=update_id + 1_000,
            raw_text=f"source {update_id}",
            received_at=NOW,
            trace_id="1" * 32,
        )
    )
    return await PostgresTaskRepository(session_factory).create(
        CreateTaskCommand(
            access_context=access_context,
            title=f"task {update_id}",
            source_capture_event_id=source.id,
            created_at=NOW,
            trace_id="1" * 32,
        )
    )


async def seed_reminder(
    engine: AsyncEngine,
    access_context: AccessContext,
    *,
    update_id: int,
    remind_at: datetime = NOW - timedelta(minutes=1),
) -> Reminder:
    task = await seed_task(engine, access_context, update_id=update_id)
    return await PostgresReminderRepository(
        create_session_factory(engine)
    ).create_reminder(
        CreateReminderCommand(
            access_context=access_context,
            remind_at=remind_at,
            text=task.title,
            source_task_id=task.id,
            created_at=NOW,
            trace_id="1" * 32,
        )
    )


@pytest.mark.asyncio
async def test_rls_hides_reminders_across_spaces_in_both_directions(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    reminder_a = await seed_reminder(engine, ACCESS_A, update_id=400)
    reminder_b = await seed_reminder(engine, ACCESS_B, update_id=401)

    # Member (B) не видит напоминание admin'а (A)…
    await _set_scope(session, ACCESS_B)
    assert (await session.scalars(select(ReminderModel.id))).all() == [reminder_b.id]
    # …и admin (A) не видит напоминание member'а (B): admin не суперпользователь.
    await _set_scope(session, ACCESS_A)
    assert (await session.scalars(select(ReminderModel.id))).all() == [reminder_a.id]


@pytest.mark.asyncio
async def test_claim_due_never_crosses_into_another_space(
    engine: AsyncEngine,
) -> None:
    await seed_reminder(engine, ACCESS_A, update_id=410)

    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        foreign_claim = await PostgresReminderWriter(session).claim_due(ACCESS_B, NOW)
        own_claim = await PostgresReminderWriter(session).claim_due(ACCESS_A, NOW)

    assert foreign_claim is None
    assert own_claim is not None


@pytest.mark.asyncio
async def test_app_role_cannot_read_or_insert_reminders_without_a_scope(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    reminder_a = await seed_reminder(engine, ACCESS_A, update_id=420)

    assert await session.scalar(select(func.count()).select_from(ReminderModel)) == 0

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(ReminderModel).values(
                id=uuid4(),
                user_space_id=ACCESS_A.user_space_id,
                remind_at=NOW,
                text="smuggled",
                status=ReminderStatus.PENDING.value,
                source_task_id=reminder_a.source_task_id,
                send_attempts=0,
                next_attempt_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_mark_sent_is_idempotent_pending_to_sent_once(
    engine: AsyncEngine,
) -> None:
    reminder = await seed_reminder(engine, ACCESS_A, update_id=430)

    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        first = await PostgresReminderWriter(session).mark_sent(
            ACCESS_A, reminder.id, NOW, telegram_message_id=555_001
        )
    async with session_factory() as session, session.begin():
        second = await PostgresReminderWriter(session).mark_sent(
            ACCESS_A,
            reminder.id,
            NOW + timedelta(seconds=1),
            telegram_message_id=555_002,
        )

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_cancel_for_task_downs_only_the_pending_reminder_of_that_task(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    cancelled = await seed_reminder(engine, ACCESS_A, update_id=440)
    untouched = await seed_reminder(engine, ACCESS_A, update_id=441)

    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await PostgresReminderWriter(session).cancel_for_task(
            CancelReminderForTaskCommand(
                access_context=ACCESS_A,
                source_task_id=cancelled.source_task_id,
                cancelled_at=NOW,
            )
        )
        claim_after = await PostgresReminderWriter(session).claim_due(ACCESS_A, NOW)

    async with create_session_factory(schema_engine)() as owner_session:
        by_id = {
            model.id: model.status
            for model in await owner_session.scalars(select(ReminderModel))
        }
    assert by_id[cancelled.id] is ReminderStatus.CANCELLED
    assert by_id[untouched.id] is ReminderStatus.PENDING
    # Отменённое больше не claim'ится; второе (другой задачи) — да.
    assert claim_after is not None
    assert claim_after.reminder_id == untouched.id


@pytest.mark.asyncio
async def test_reminders_table_has_forced_row_level_security(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = to_regclass(:reminders)"
            ),
            {"reminders": f"{isolated_database.schema}.reminders"},
        )

    assert result.all() == [(True, True)]


@pytest.mark.asyncio
async def test_init_db_adds_telegram_message_id_to_an_existing_reminders_table(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    # Живая прод-база уже несёт reminders БЕЗ telegram_message_id;
    # create_all(checkfirst=True) существующую таблицу не трогает — колонку
    # обязан идемпотентно дорастить init-db (ADD COLUMN IF NOT EXISTS).
    table = f'"{isolated_database.schema}".reminders'
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(f"ALTER TABLE {table} DROP COLUMN telegram_message_id")
        )

    await initialize_schema(schema_engine, isolated_database.schema)

    async with schema_engine.connect() as connection:
        column_type = await connection.scalar(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = 'reminders' "
                "AND column_name = 'telegram_message_id'"
            ),
            {"schema": isolated_database.schema},
        )
    assert column_type == "bigint"


@pytest.mark.asyncio
async def test_app_role_can_update_but_not_delete_reminders(
    session: AsyncSession,
) -> None:
    update_allowed = await session.scalar(
        text("SELECT has_table_privilege(current_user, 'reminders', 'UPDATE')")
    )
    delete_allowed = await session.scalar(
        text("SELECT has_table_privilege(current_user, 'reminders', 'DELETE')")
    )

    assert update_allowed is True
    assert delete_allowed is False


async def _set_scope(session: AsyncSession, access_context: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

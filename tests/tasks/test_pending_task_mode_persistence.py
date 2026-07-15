from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
)
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresPendingCaptureSelectionRepository,
)
from second_brain.slices.tasks.application.contracts import (
    ConsumePendingTaskTextCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.application.task_capture import TaskCapture
from second_brain.slices.tasks.domain.entities import PendingCaptureType, TaskStatus
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
async def reset_task_schema(
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
                    "role": "member",
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


def set_awaiting_command(access_context: AccessContext) -> SetAwaitingTaskCommand:
    return SetAwaitingTaskCommand(
        access_context=access_context,
        updated_at=NOW,
        trace_id="1" * 32,
    )


def capture_command(access_context: AccessContext) -> CaptureTextCommand:
    return CaptureTextCommand(
        access_context=access_context,
        bot_id=100,
        telegram_update_id=200,
        telegram_message_id=300,
        raw_text="private task",
        received_at=NOW,
        trace_id="1" * 32,
    )


def consume_command(source_capture_event_id: UUID) -> ConsumePendingTaskTextCommand:
    return ConsumePendingTaskTextCommand(
        access_context=ACCESS_A,
        text="  Preserve this title exactly  ",
        is_private_chat=True,
        telegram_message_id=300,
        source_capture_event_id=source_capture_event_id,
        created_at=NOW,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
async def test_missing_pending_selection_defaults_to_note(
    engine: AsyncEngine,
) -> None:
    repository = PostgresPendingCaptureSelectionRepository(
        create_session_factory(engine)
    )

    selection = await repository.consume_selection(
        consume_command(UUID("00000000-0000-0000-0000-000000000101"))
    )

    assert selection is PendingCaptureType.NOTE


@pytest.mark.asyncio
async def test_pending_mode_survives_repository_restart_then_consumes_once(
    engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(engine)
    first_process = TaskCapture(
        PostgresPendingCaptureSelectionRepository(session_factory)
    )

    await first_process.set_awaiting_task(set_awaiting_command(ACCESS_A))

    source = await PostgresCaptureEventRepository(session_factory).create(
        capture_command(ACCESS_A)
    )
    second_process = TaskCapture(
        PostgresPendingCaptureSelectionRepository(session_factory)
    )
    task = await second_process.consume_for_text(consume_command(source.id))
    repeated_task = await second_process.consume_for_text(consume_command(source.id))

    assert task is not None
    assert task.title == "  Preserve this title exactly  "
    assert task.description is None
    assert task.status is TaskStatus.INBOX
    assert repeated_task is None


@pytest.mark.asyncio
async def test_pending_mode_row_is_scoped_by_row_level_security(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    repository = PostgresPendingCaptureSelectionRepository(
        create_session_factory(engine)
    )
    await repository.set_awaiting_task(set_awaiting_command(ACCESS_B))

    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(ACCESS_A.user_space_id)},
    )

    assert (await session.scalars(select(PendingCaptureSelectionModel))).all() == []
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(PendingCaptureSelectionModel).values(
                user_space_id=ACCESS_B.user_space_id,
                selection=PendingCaptureType.TASK.value,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
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
async def reset_capture_schema(
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
                    "role": "admin",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_id,
                    "role": "admin",
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


@pytest_asyncio.fixture
async def app_repository(engine: AsyncEngine) -> PostgresCaptureEventRepository:
    return PostgresCaptureEventRepository(create_session_factory(engine))


def command_for(
    access_context: AccessContext, *, update_id: int, raw_text: str
) -> CaptureTextCommand:
    return CaptureTextCommand(
        access_context=access_context,
        bot_id=100,
        telegram_update_id=update_id,
        telegram_message_id=update_id + 1000,
        raw_text=raw_text,
        received_at=NOW,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
async def test_rls_hides_space_b_from_space_a(
    app_repository: PostgresCaptureEventRepository,
) -> None:
    event_a = await app_repository.create(
        command_for(ACCESS_A, update_id=200, raw_text="a")
    )
    await app_repository.create(command_for(ACCESS_B, update_id=201, raw_text="b"))

    assert await app_repository.list_recent(ACCESS_A) == [event_a]
    assert await app_repository.count(ACCESS_A) == 1


@pytest.mark.asyncio
async def test_app_role_cannot_insert_space_b_while_scoped_to_space_a(
    session: AsyncSession,
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(ACCESS_A.user_space_id)},
    )

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(CaptureEventModel).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=202,
                telegram_message_id=1202,
                raw_text="b",
                received_at=NOW,
                created_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
async def test_app_role_cannot_read_or_insert_without_a_scope(
    app_repository: PostgresCaptureEventRepository,
    session: AsyncSession,
) -> None:
    await app_repository.create(command_for(ACCESS_A, update_id=203, raw_text="a"))

    assert await session.scalar(select(CaptureEventModel.id).limit(1)) is None
    count = await session.scalar(
        select(text("count(*)")).select_from(CaptureEventModel)
    )
    assert count == 0

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(CaptureEventModel).values(
                id=uuid4(),
                user_space_id=ACCESS_A.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=206,
                telegram_message_id=1206,
                raw_text="a",
                received_at=NOW,
                created_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
async def test_app_role_cannot_update_or_delete_a_capture_event(
    app_repository: PostgresCaptureEventRepository,
    session: AsyncSession,
) -> None:
    event = await app_repository.create(
        command_for(ACCESS_A, update_id=204, raw_text="a")
    )
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(ACCESS_A.user_space_id)},
    )

    with pytest.raises(DBAPIError):
        await session.execute(
            update(CaptureEventModel)
            .where(CaptureEventModel.id == event.id)
            .values(raw_text="changed")
        )
    await session.rollback()

    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(ACCESS_A.user_space_id)},
    )
    with pytest.raises(DBAPIError):
        await session.execute(
            delete(CaptureEventModel).where(CaptureEventModel.id == event.id)
        )


@pytest.mark.asyncio
async def test_postgres_rejects_duplicate_telegram_delivery(
    app_repository: PostgresCaptureEventRepository,
) -> None:
    await app_repository.create(command_for(ACCESS_A, update_id=205, raw_text="a"))

    with pytest.raises(IntegrityError):
        await app_repository.create(command_for(ACCESS_A, update_id=205, raw_text="a"))


@pytest.mark.asyncio
async def test_capture_event_table_has_forced_row_level_security(
    schema_engine: AsyncEngine,
    isolated_database: IsolatedDatabase,
) -> None:
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid = to_regclass(:table_name)"
            ),
            {"table_name": f"{isolated_database.schema}.capture_events"},
        )

    assert result.one() == (True, True)

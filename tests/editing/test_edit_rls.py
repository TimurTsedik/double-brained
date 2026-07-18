"""Изоляция pending_edit_modes: forced RLS + same-space негативы двумя юзерами."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.editing.adapters.persistence.models import (
    PendingEditModeModel,
)
from second_brain.slices.editing.adapters.persistence.repository import (
    LockedPendingEdit,
    PostgresPendingEditWriter,
)
from second_brain.slices.editing.application.contracts import BeginRecordEditCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
TRACE_A = "a" * 32
TRACE_B = "b" * 32
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)
RECORD_A = UUID("00000000-0000-0000-0000-000000000301")
RECORD_B = UUID("00000000-0000-0000-0000-000000000302")


@pytest_asyncio.fixture(autouse=True)
async def reset_editing_schema(
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
                    "id": access.user_id,
                    "role": "admin" if access == ACCESS_A else "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS_A, ACCESS_B)
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": access.user_space_id,
                    "owner_user_id": access.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS_A, ACCESS_B)
            ],
        )


def _begin_command(
    access: AccessContext, record_id: UUID, trace_id: str
) -> BeginRecordEditCommand:
    return BeginRecordEditCommand(
        access_context=access,
        record_kind=SearchRecordType.NOTE,
        record_id=record_id,
        updated_at=NOW,
        trace_id=trace_id,
    )


async def _set_pending(
    engine: AsyncEngine, access: AccessContext, record_id: UUID, trace_id: str
) -> None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresPendingEditWriter(session).set_pending(
                _begin_command(access, record_id, trace_id)
            )


async def _scope_to(session: AsyncSession, access: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :value, true)"),
        {"value": str(access.user_space_id)},
    )


@pytest.mark.asyncio
async def test_pending_edit_rows_are_isolated_between_spaces(
    engine: AsyncEngine,
) -> None:
    await _set_pending(engine, ACCESS_A, RECORD_A, TRACE_A)
    await _set_pending(engine, ACCESS_B, RECORD_B, TRACE_B)

    async with create_session_factory(engine)() as session:
        async with session.begin():
            writer = PostgresPendingEditWriter(session)
            assert await writer.lock_pending(ACCESS_A) == LockedPendingEdit(
                record_kind=SearchRecordType.NOTE, record_id=RECORD_A
            )
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await _scope_to(session, ACCESS_B)
            rows = (await session.execute(select(PendingEditModeModel))).scalars().all()
    assert [(row.user_space_id, row.record_id) for row in rows] == [
        (ACCESS_B.user_space_id, RECORD_B)
    ]


@pytest.mark.asyncio
async def test_cancel_of_one_space_never_touches_the_other(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _set_pending(engine, ACCESS_A, RECORD_A, TRACE_A)
    await _set_pending(engine, ACCESS_B, RECORD_B, TRACE_B)

    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresPendingEditWriter(session).cancel(ACCESS_A)

    async with create_session_factory(schema_engine)() as session:
        rows = (await session.execute(select(PendingEditModeModel))).scalars().all()
    assert [row.user_space_id for row in rows] == [ACCESS_B.user_space_id]


@pytest.mark.asyncio
async def test_session_scoped_to_one_space_cannot_write_into_another(
    engine: AsyncEngine,
) -> None:
    # WITH CHECK forced-RLS политики: сессия в скоупе A не вставит строку B.
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await _scope_to(session, ACCESS_A)
            with pytest.raises(DBAPIError):
                await session.execute(
                    insert(PendingEditModeModel).values(
                        user_space_id=ACCESS_B.user_space_id,
                        record_kind=SearchRecordType.NOTE,
                        record_id=RECORD_B,
                        updated_at=NOW,
                        trace_id=TRACE_B,
                    )
                )


@pytest.mark.asyncio
async def test_unscoped_application_session_sees_no_pending_edits(
    engine: AsyncEngine,
) -> None:
    await _set_pending(engine, ACCESS_A, RECORD_A, TRACE_A)

    async with create_session_factory(engine)() as session:
        count = await session.scalar(
            select(func.count()).select_from(PendingEditModeModel)
        )
    assert count == 0

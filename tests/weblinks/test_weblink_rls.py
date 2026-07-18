"""Изоляция weblink-таблиц: forced RLS + same-space негативы двумя юзерами."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.weblinks.adapters.persistence.models import (
    PageTitleModel,
    RecordUrlModel,
)
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresWeblinkWriter,
)
from second_brain.slices.weblinks.application.contracts import (
    RecordUrlEntry,
    SaveRecordLinksCommand,
)
from second_brain.slices.weblinks.domain.entities import WeblinkRecordKind
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
async def reset_weblink_schema(
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


def links_command(access_context: AccessContext, url: str) -> SaveRecordLinksCommand:
    return SaveRecordLinksCommand(
        access_context=access_context,
        record_kind=WeblinkRecordKind.NOTE,
        record_id=uuid4(),
        entries=(RecordUrlEntry(label="тут", url=url),),
        created_at=NOW,
        trace_id="1" * 32,
    )


async def save_links(
    engine: AsyncEngine, access_context: AccessContext, url: str
) -> None:
    async with create_session_factory(engine)() as session, session.begin():
        await PostgresWeblinkWriter(session).save_links(
            links_command(access_context, url)
        )


async def _set_scope(session: AsyncSession, access_context: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


@pytest.mark.asyncio
async def test_record_urls_and_page_titles_are_scoped_to_their_user_space(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await save_links(engine, ACCESS_A, "https://a.example/one")
    await save_links(engine, ACCESS_B, "https://b.example/two")

    await _set_scope(session, ACCESS_A)
    assert (await session.scalars(select(RecordUrlModel.url))).all() == [
        "https://a.example/one"
    ]
    assert (await session.scalars(select(PageTitleModel.original_url))).all() == [
        "https://a.example/one"
    ]

    await _set_scope(session, ACCESS_B)
    assert (await session.scalars(select(RecordUrlModel.url))).all() == [
        "https://b.example/two"
    ]
    assert (await session.scalars(select(PageTitleModel.original_url))).all() == [
        "https://b.example/two"
    ]


@pytest.mark.asyncio
async def test_weblink_rows_are_hidden_without_scope(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await save_links(engine, ACCESS_A, "https://a.example/hidden")

    assert await session.scalar(select(func.count()).select_from(RecordUrlModel)) == 0
    assert await session.scalar(select(func.count()).select_from(PageTitleModel)) == 0


@pytest.mark.asyncio
async def test_foreign_user_space_cannot_insert_weblink_rows(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await _set_scope(session, ACCESS_A)
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(RecordUrlModel).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                record_kind=WeblinkRecordKind.NOTE.value,
                record_id=uuid4(),
                position=0,
                label="x",
                url="https://x.example",
                created_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()
    await _set_scope(session, ACCESS_A)
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(PageTitleModel).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                original_url="https://x.example",
                normalized_url="https://x.example",
                title=None,
                status="pending",
                attempt_count=0,
                next_attempt_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
async def test_weblink_tables_have_forced_row_level_security(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class JOIN pg_namespace ON pg_namespace.oid = relnamespace "
                "WHERE relname = ANY(:table_names) AND nspname = :schema "
                "ORDER BY relname"
            ),
            {
                "table_names": ["record_urls", "page_titles"],
                "schema": isolated_database.schema,
            },
        )

    assert result.all() == [
        ("page_titles", True, True),
        ("record_urls", True, True),
    ]

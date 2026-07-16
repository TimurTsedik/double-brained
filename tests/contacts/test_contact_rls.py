from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.contacts.adapters.persistence.models import ContactModel
from second_brain.slices.contacts.adapters.persistence.repository import (
    PostgresContactWriter,
)
from second_brain.slices.contacts.application.contracts import SaveContactCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_contact_schema(
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
                    # A = admin, B = member: admin НЕ суперпользователь, RLS
                    # изолирует по user_space_id в обе стороны.
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


async def seed_contact(
    engine: AsyncEngine,
    access_context: AccessContext,
    *,
    name: str,
    phone: str,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await PostgresContactWriter(session).upsert_contact(
            SaveContactCommand(
                access_context=access_context,
                display_name=name,
                phone_number=phone,
                saved_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
async def test_rls_hides_contacts_across_spaces_in_both_directions(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-1")
    await seed_contact(engine, ACCESS_B, name="Маша", phone="+972-2")

    await _set_scope(session, ACCESS_B)
    names_b = (await session.scalars(select(ContactModel.display_name))).all()
    assert names_b == ["Маша"]
    await _set_scope(session, ACCESS_A)
    names_a = (await session.scalars(select(ContactModel.display_name))).all()
    assert names_a == ["Ави"]


@pytest.mark.asyncio
async def test_list_contacts_never_crosses_into_another_space(
    engine: AsyncEngine,
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-1")

    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        foreign = await PostgresContactWriter(session).list_contacts(ACCESS_B)
        own = await PostgresContactWriter(session).list_contacts(ACCESS_A)

    assert foreign == ()
    assert [contact.display_name for contact in own] == ["Ави"]


@pytest.mark.asyncio
async def test_app_role_cannot_read_or_insert_contacts_without_a_scope(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-1")

    assert await session.scalar(select(func.count()).select_from(ContactModel)) == 0

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(ContactModel).values(
                id=uuid4(),
                user_space_id=ACCESS_A.user_space_id,
                display_name="smuggled",
                phone_number="+000",
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_contacts_table_has_forced_row_level_security(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = to_regclass(:contacts)"
            ),
            {"contacts": f"{isolated_database.schema}.contacts"},
        )

    assert result.all() == [(True, True)]


@pytest.mark.asyncio
async def test_app_role_can_update_but_not_delete_contacts(
    session: AsyncSession,
) -> None:
    for privilege, expected in (
        ("SELECT", True),
        ("INSERT", True),
        ("UPDATE", True),
        ("DELETE", False),
    ):
        allowed = await session.scalar(
            text("SELECT has_table_privilege(current_user, 'contacts', :privilege)"),
            {"privilege": privilege},
        )
        assert allowed is expected, privilege


async def _set_scope(session: AsyncSession, access_context: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

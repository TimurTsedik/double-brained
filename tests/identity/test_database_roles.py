import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
)
from tests.identity.conftest import IsolatedDatabase


@pytest.mark.asyncio
async def test_application_role_is_not_superuser_or_bypassrls(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
            "rolreplication, rolinherit "
            "FROM pg_roles WHERE rolname = current_user"
        )
    )

    assert result.one() == (False, False, False, False, False, False)


@pytest.mark.asyncio
async def test_application_role_has_no_delete_privilege(
    session: AsyncSession,
) -> None:
    has_delete_privilege = await session.scalar(
        text("SELECT has_table_privilege(current_user, 'users', 'DELETE')")
    )

    assert has_delete_privilege is False


@pytest.mark.asyncio
async def test_application_role_can_mutate_only_pending_search_state(
    session: AsyncSession,
) -> None:
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert (
            await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, "
                    "'pending_search_modes', :privilege)"
                ),
                {"privilege": privilege},
            )
            is True
        )

    for table_name in ("notes", "ideas", "decisions", "questions"):
        for privilege in ("UPDATE", "DELETE"):
            assert (
                await session.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :table_name, "
                        ":privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                )
                is False
            )


@pytest.mark.asyncio
async def test_application_role_can_only_read_and_append_semantic_index_tables(
    session: AsyncSession,
) -> None:
    expected = {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False}

    for table_name in ("semantic_documents", "indexing_targets"):
        for privilege, allowed in expected.items():
            actual = await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table_name, :privilege)"
                ),
                {"table_name": table_name, "privilege": privilege},
            )

            assert actual is allowed, (table_name, privilege)


@pytest.mark.asyncio
async def test_application_role_lacks_update_on_immutable_identity_tables(
    session: AsyncSession,
) -> None:
    immutable_tables = (
        "users",
        "user_spaces",
        "telegram_identities",
        "telegram_update_receipts",
    )

    for table_name in immutable_tables:
        has_update_privilege = await session.scalar(
            text("SELECT has_table_privilege(current_user, :table_name, 'UPDATE')"),
            {"table_name": table_name},
        )

        assert has_update_privilege is False


@pytest.mark.asyncio
async def test_application_runtime_rejects_an_owner_role(
    schema_engine: AsyncEngine,
) -> None:
    with pytest.raises(RuntimeError, match="non-superuser"):
        await assert_non_privileged_application_role(schema_engine)


@pytest.mark.asyncio
async def test_application_runtime_rejects_a_nonprivileged_role_that_is_not_app_role(
    isolated_database: IsolatedDatabase,
    schema_engine: AsyncEngine,
) -> None:
    role_name = "second_brain_untrusted_test_role"
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f"CREATE ROLE {role_name} LOGIN NOSUPERUSER NOBYPASSRLS "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT"
            )
        )

    database_url = make_url(isolated_database.schema_database_url).set(
        username=role_name
    )
    untrusted_engine = create_database_engine(
        database_url.render_as_string(hide_password=False)
    )
    try:
        with pytest.raises(RuntimeError, match="second_brain_app"):
            await assert_non_privileged_application_role(untrusted_engine)
    finally:
        await untrusted_engine.dispose()
        async with schema_engine.begin() as connection:
            await connection.execute(text(f"DROP ROLE {role_name}"))


@pytest.mark.asyncio
async def test_application_runtime_accepts_the_dedicated_app_role(
    engine: AsyncEngine,
) -> None:
    await assert_non_privileged_application_role(engine)

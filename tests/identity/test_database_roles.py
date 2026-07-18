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
async def test_application_role_can_fully_mutate_pending_mode_state(
    session: AsyncSession,
) -> None:
    # Транзиентные UI-режимы (поиск / правка): полный CRUD — установка,
    # потребление и отмена удаляют/переписывают строку.
    for table_name in ("pending_search_modes", "pending_edit_modes"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert (
                await session.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :table_name, "
                        ":privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                )
                is True
            ), (table_name, privilege)


@pytest.mark.asyncio
async def test_application_role_updates_only_text_columns_of_knowledge_records(
    session: AsyncSession,
) -> None:
    # Правка (S3): КОЛОНОЧНЫЙ UPDATE text+updated_at; происхождение записи
    # (created_at/trace_id/source/space) app-роль переписать не может. DELETE
    # по-прежнему нет.
    for table_name in ("notes", "ideas", "decisions", "questions"):
        # Грант колоночный: has_ANY_column_privilege (табличного UPDATE нет).
        assert (
            await session.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user, :table_name, "
                    "'UPDATE')"
                ),
                {"table_name": table_name},
            )
            is True
        ), table_name
        for column, allowed in (
            ("text", True),
            ("updated_at", True),
            ("edited_at", True),
            ("created_at", False),
            ("trace_id", False),
            ("source_capture_event_id", False),
            ("user_space_id", False),
            ("id", False),
        ):
            actual = await session.scalar(
                text(
                    "SELECT has_column_privilege(current_user, :table_name, "
                    ":column, 'UPDATE')"
                ),
                {"table_name": table_name, "column": column},
            )
            assert actual is allowed, (table_name, column)
        assert (
            await session.scalar(
                text("SELECT has_table_privilege(current_user, :table_name, 'DELETE')"),
                {"table_name": table_name},
            )
            is False
        ), table_name


@pytest.mark.asyncio
async def test_application_role_updates_only_mutable_task_columns(
    session: AsyncSession,
) -> None:
    # tasks: КОЛОНОЧНЫЙ UPDATE (title, status, updated_at, edited_at) —
    # complete двигает status+updated_at, правка — title+updated_at+edited_at.
    # Происхождение (created_at/trace_id/source/space) и description app-роль
    # переписать не может; DELETE нет.
    assert (
        await session.scalar(
            text("SELECT has_any_column_privilege(current_user, 'tasks', 'UPDATE')")
        )
        is True
    )
    for column, allowed in (
        ("title", True),
        ("status", True),
        ("updated_at", True),
        ("edited_at", True),
        ("description", False),
        ("created_at", False),
        ("trace_id", False),
        ("source_capture_event_id", False),
        ("user_space_id", False),
        ("id", False),
    ):
        actual = await session.scalar(
            text(
                "SELECT has_column_privilege(current_user, 'tasks', :column, 'UPDATE')"
            ),
            {"column": column},
        )
        assert actual is allowed, column
    assert (
        await session.scalar(
            text("SELECT has_table_privilege(current_user, 'tasks', 'DELETE')")
        )
        is False
    )


@pytest.mark.asyncio
async def test_application_role_privileges_on_semantic_index_tables(
    session: AsyncSession,
) -> None:
    # semantic_documents: DELETE есть — правка (S3) атомарно заменяет чанки
    # записи (delete+insert одной транзакцией шага). indexing_targets —
    # append-only журнал целей: без UPDATE/DELETE.
    expected = {
        "semantic_documents": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": True,
        },
        "indexing_targets": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        },
    }

    for table_name, privileges in expected.items():
        for privilege, allowed in privileges.items():
            actual = await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table_name, :privilege)"
                ),
                {"table_name": table_name, "privilege": privilege},
            )

            assert actual is allowed, (table_name, privilege)


@pytest.mark.asyncio
async def test_application_role_privileges_on_weblink_tables(
    session: AsyncSession,
) -> None:
    # record_urls: DELETE только ради правки (S3) — набор ссылок записи
    # пересобирается целиком; UPDATE нет. page_titles: очередь/кэш титулов —
    # INSERT+UPDATE, без DELETE.
    expected = {
        "record_urls": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": True,
        },
        "page_titles": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": True,
            "DELETE": False,
        },
    }

    for table_name, privileges in expected.items():
        for privilege, allowed in privileges.items():
            actual = await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table_name, :privilege)"
                ),
                {"table_name": table_name, "privilege": privilege},
            )

            assert actual is allowed, (table_name, privilege)


@pytest.mark.asyncio
async def test_application_role_privileges_on_memory_tables(
    session: AsyncSession,
) -> None:
    expected = {
        "pending_memory_questions": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": True,
            "DELETE": True,
        },
        "memory_answer_steps": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": True,
            "DELETE": False,
        },
        "memory_questions": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        },
        "memory_answer_runs": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        },
        "memory_run_evidence": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        },
        "memory_answers": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        },
        "memory_answer_sources": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        },
    }
    for table_name, privileges in expected.items():
        for privilege, allowed in privileges.items():
            actual = await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table_name, :privilege)"
                ),
                {"table_name": table_name, "privilege": privilege},
            )
            assert actual is allowed, (table_name, privilege)


@pytest.mark.asyncio
async def test_application_role_privileges_on_telegram_update_inbox(
    session: AsyncSession,
) -> None:
    # Webhook-INBOX: роут ставит апдейт (INSERT), inbox-шаг клеймит и завершает
    # (SELECT + КОЛОНОЧНЫЙ UPDATE). Удалять историю app-роль не может.
    for privilege, allowed in (
        ("SELECT", True),
        ("INSERT", True),
        ("UPDATE", False),
        ("DELETE", False),
    ):
        actual = await session.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'telegram_update_inbox', :privilege)"
            ),
            {"privilege": privilege},
        )

        assert actual is allowed, privilege

    # Меняется только прогресс обработки; сам апдейт (payload/bot_id/update_id/
    # trace_id) и время приёма app-роль переписать не может.
    assert (
        await session.scalar(
            text(
                "SELECT has_any_column_privilege(current_user, "
                "'telegram_update_inbox', 'UPDATE')"
            )
        )
        is True
    )
    for column, allowed in (
        ("status", True),
        ("attempt_count", True),
        ("next_attempt_at", True),
        ("payload", False),
        ("bot_id", False),
        ("update_id", False),
        ("trace_id", False),
        ("received_at", False),
        ("id", False),
    ):
        actual = await session.scalar(
            text(
                "SELECT has_column_privilege(current_user, "
                "'telegram_update_inbox', :column, 'UPDATE')"
            ),
            {"column": column},
        )

        assert actual is allowed, column


@pytest.mark.asyncio
async def test_application_role_lacks_update_on_immutable_identity_tables(
    session: AsyncSession,
) -> None:
    immutable_tables = (
        "users",
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
async def test_application_role_can_update_only_language_columns_on_user_spaces(
    session: AsyncSession,
) -> None:
    # КОЛОНОЧНЫЙ грант: смена языка бампает updated_at, но право менять
    # owner_user_id/timezone/is_active app-роли не даётся.
    updatable = ("language", "updated_at")
    immutable = ("owner_user_id", "timezone", "is_active", "id", "created_at")

    for column in updatable:
        allowed = await session.scalar(
            text(
                "SELECT has_column_privilege(current_user, 'user_spaces', "
                ":column, 'UPDATE')"
            ),
            {"column": column},
        )
        assert allowed is True, column

    for column in immutable:
        allowed = await session.scalar(
            text(
                "SELECT has_column_privilege(current_user, 'user_spaces', "
                ":column, 'UPDATE')"
            ),
            {"column": column},
        )
        assert allowed is False, column

    has_delete = await session.scalar(
        text("SELECT has_table_privilege(current_user, 'user_spaces', 'DELETE')")
    )
    assert has_delete is False


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

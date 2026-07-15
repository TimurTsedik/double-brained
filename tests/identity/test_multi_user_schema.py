"""Реконсиляция много-пользовательских ограничений identity (M1/M9/M10).

Живая прод-база слайсов 1–3 несёт ОДНОпользовательские замки: role='admin'
CHECK'и, created_by_actor='bootstrap_cli' CHECK и частичный уникальный индекс
«один pending». initialize_identity_schema обязана снять их идемпотентно (не падая
на повторе) и привести схему к много-пользовательскому виду. Каждый тест работает
в СВОЕЙ одноразовой схеме — общий session-scoped schema не мутируем.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    initialize_identity_schema,
)
from tests.identity.conftest import IsolatedDatabase

TS = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def fresh_schema(
    isolated_database: IsolatedDatabase,
) -> AsyncIterator[tuple[AsyncEngine, str]]:
    schema = f"test_multi_user_{uuid4().hex}"
    database_engine = create_database_engine(isolated_database.schema_database_url)
    schema_engine = database_engine.execution_options(
        schema_translate_map={None: schema}
    )
    try:
        async with database_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await initialize_identity_schema(schema_engine, schema)
        yield schema_engine, schema
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await database_engine.dispose()


async def _install_legacy_single_user_locks(
    schema_engine: AsyncEngine, schema: str
) -> None:
    users = f'"{schema}".users'
    invites = f'"{schema}".enrollment_invites'
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(f'DROP INDEX IF EXISTS "{schema}".uq_users_active_admin')
        )
        await connection.execute(
            text(f"ALTER TABLE {users} DROP CONSTRAINT ck_users_role_admin")
        )
        await connection.execute(
            text(
                f"ALTER TABLE {users} ADD CONSTRAINT ck_users_role_admin "
                "CHECK (role = 'admin')"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {invites} "
                "DROP CONSTRAINT ck_enrollment_invites_role_admin"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {invites} "
                "ADD CONSTRAINT ck_enrollment_invites_role_admin "
                "CHECK (role = 'admin')"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {invites} "
                "DROP CONSTRAINT ck_enrollment_invites_bootstrap_actor"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {invites} "
                "ADD CONSTRAINT ck_enrollment_invites_bootstrap_actor "
                "CHECK (created_by_actor = 'bootstrap_cli')"
            )
        )
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_enrollment_invites_pending_bootstrap "
                f"ON {invites} (status) WHERE status = 'pending'"
            )
        )


async def _insert_user(
    schema_engine: AsyncEngine, schema: str, role: str, is_active: bool = True
) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".users '
                "(id, role, is_active, created_at, updated_at) "
                "VALUES (:id, :role, :active, :ts, :ts)"
            ),
            {"id": uuid4(), "role": role, "active": is_active, "ts": TS},
        )


async def _insert_pending_invite(
    schema_engine: AsyncEngine,
    schema: str,
    *,
    role: str,
    created_by_actor: str,
    token_hash: bytes,
) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".enrollment_invites '
                "(id, token_hash, pepper_key_id, role, status, created_by_actor, "
                "created_at, expires_at) VALUES "
                "(:id, :hash, 'k1', :role, 'pending', :actor, :ts, :ts)"
            ),
            {
                "id": uuid4(),
                "hash": token_hash,
                "role": role,
                "actor": created_by_actor,
                "ts": TS,
            },
        )


async def _assert_multi_user_final_state(
    schema_engine: AsyncEngine, schema: str
) -> None:
    # member-роль разрешена для User и invite.
    await _insert_user(schema_engine, schema, "member")
    await _insert_pending_invite(
        schema_engine,
        schema,
        role="member",
        created_by_actor="admin_bot",
        token_hash=b"m",
    )
    # created_by_actor='admin_bot' допустим; несколько pending сосуществуют.
    await _insert_pending_invite(
        schema_engine,
        schema,
        role="admin",
        created_by_actor="bootstrap_cli",
        token_hash=b"a",
    )
    # result_kind CHECK несёт новые виды.
    async with schema_engine.begin() as connection:
        for update_id, kind in (
            (1, "invite_created"),
            (2, "invite_forbidden"),
            (3, "already_enrolled"),
        ):
            await connection.execute(
                text(
                    f'INSERT INTO "{schema}".telegram_update_receipts '
                    "(bot_id, update_id, result_kind, trace_id, created_at) "
                    "VALUES (1, :upd, :kind, :trace, :ts)"
                ),
                {"upd": update_id, "kind": kind, "trace": "a" * 32, "ts": TS},
            )
    # Один активный admin: первый проходит, второй ловится уникальным индексом.
    await _insert_user(schema_engine, schema, "admin")
    with pytest.raises(DBAPIError):
        await _insert_user(schema_engine, schema, "admin")


@pytest.mark.asyncio
async def test_reconciles_legacy_single_user_locks_idempotently(
    fresh_schema: tuple[AsyncEngine, str],
) -> None:
    schema_engine, schema = fresh_schema
    await _install_legacy_single_user_locks(schema_engine, schema)

    # Повторный прогон на «живой» базе со старыми замками не падает и чинит схему.
    await initialize_identity_schema(schema_engine, schema)
    await initialize_identity_schema(schema_engine, schema)

    await _assert_multi_user_final_state(schema_engine, schema)


@pytest.mark.asyncio
async def test_fresh_init_matches_reconciled_final_state(
    fresh_schema: tuple[AsyncEngine, str],
) -> None:
    schema_engine, schema = fresh_schema
    # init-db на ПУСТОЙ базе не воссоздаёт легаси-замки и даёт тот же итог.
    await initialize_identity_schema(schema_engine, schema)

    await _assert_multi_user_final_state(schema_engine, schema)


@pytest.mark.asyncio
async def test_multiple_pending_invites_coexist_after_reconcile(
    fresh_schema: tuple[AsyncEngine, str],
) -> None:
    schema_engine, schema = fresh_schema
    await _install_legacy_single_user_locks(schema_engine, schema)
    await initialize_identity_schema(schema_engine, schema)

    # Легаси-индекс «один pending» снят: два pending с разными token_hash уживаются.
    await _insert_pending_invite(
        schema_engine,
        schema,
        role="member",
        created_by_actor="admin_bot",
        token_hash=b"x",
    )
    await _insert_pending_invite(
        schema_engine,
        schema,
        role="member",
        created_by_actor="admin_bot",
        token_hash=b"y",
    )

    async with schema_engine.begin() as connection:
        pending = await connection.scalar(
            text(
                f'SELECT count(*) FROM "{schema}".enrollment_invites '
                "WHERE status = 'pending'"
            )
        )
    assert pending == 2

"""Реконсиляция CHECK result_kind при инициализации на СУЩЕСТВУЮЩЕЙ базе.

Живая прод-база слайсов 1–2 уже содержит telegram_update_receipts со СТАРЫМ
CHECK (без memory_*). create_all(checkfirst=True) такую таблицу не трогает, поэтому
первый `memory:ask` записывал бы receipt result_kind='memory_mode_set' и падал на
старом ограничении. initialize_identity_schema обязана идемпотентно чинить CHECK.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.slices.identity.adapters.persistence.schema import (
    initialize_identity_schema,
)
from tests.identity.conftest import IsolatedDatabase

TS = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_initialize_reconciles_result_kind_check_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    table = f'"{schema}".telegram_update_receipts'

    # Имитируем живую базу: подменяем CHECK на устаревший набор без memory_*.
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP CONSTRAINT ck_telegram_update_receipts_result_kind"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "ADD CONSTRAINT ck_telegram_update_receipts_result_kind "
                "CHECK (result_kind IN ('captured', 'ignored'))"
            )
        )

    # Повторная инициализация на существующей схеме обязана починить ограничение.
    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {table} "
                "(bot_id, update_id, result_kind, trace_id, created_at) "
                "VALUES (:bot, :upd, 'memory_mode_set', :trace, :ts)"
            ),
            {"bot": 1, "upd": 1, "trace": "a" * 32, "ts": TS},
        )
        stored = await connection.scalar(
            text(f"SELECT result_kind FROM {table} WHERE bot_id = 1 AND update_id = 1")
        )
    assert stored == "memory_mode_set"


@pytest.mark.asyncio
async def test_initialize_reconciles_language_kinds_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    table = f'"{schema}".telegram_update_receipts'

    # Живая база до Task 6: CHECK без language_prompt_shown/language_selected.
    async with schema_engine.begin() as connection:
        await connection.execute(text(f"DELETE FROM {table}"))
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP CONSTRAINT ck_telegram_update_receipts_result_kind"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "ADD CONSTRAINT ck_telegram_update_receipts_result_kind "
                "CHECK (result_kind IN ('captured', 'ignored'))"
            )
        )

    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.begin() as connection:
        for update_id, kind in (
            (10, "language_prompt_shown"),
            (11, "language_selected"),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(bot_id, update_id, result_kind, trace_id, created_at) "
                    "VALUES (:bot, :upd, :kind, :trace, :ts)"
                ),
                {"bot": 1, "upd": update_id, "kind": kind, "trace": "b" * 32, "ts": TS},
            )
        stored = await connection.scalars(
            text(f"SELECT result_kind FROM {table} ORDER BY update_id")
        )
    assert set(stored.all()) == {"language_prompt_shown", "language_selected"}


@pytest.mark.asyncio
async def test_initialize_reconciles_record_shown_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    table = f'"{schema}".telegram_update_receipts'

    # Живая база до слайса «показать целиком»: CHECK без record_shown.
    async with schema_engine.begin() as connection:
        await connection.execute(text(f"DELETE FROM {table}"))
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP CONSTRAINT ck_telegram_update_receipts_result_kind"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "ADD CONSTRAINT ck_telegram_update_receipts_result_kind "
                "CHECK (result_kind IN ('captured', 'ignored'))"
            )
        )

    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {table} "
                "(bot_id, update_id, result_kind, trace_id, created_at) "
                "VALUES (:bot, :upd, 'record_shown', :trace, :ts)"
            ),
            {"bot": 1, "upd": 20, "trace": "c" * 32, "ts": TS},
        )
        stored = await connection.scalar(
            text(f"SELECT result_kind FROM {table} WHERE bot_id = 1 AND update_id = 20")
        )
    assert stored == "record_shown"


@pytest.mark.asyncio
async def test_initialize_reconciles_edit_kinds_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    table = f'"{schema}".telegram_update_receipts'

    # Живая база до слайса «правка записи» (S3): CHECK без edit_*/record_edited.
    async with schema_engine.begin() as connection:
        await connection.execute(text(f"DELETE FROM {table}"))
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP CONSTRAINT ck_telegram_update_receipts_result_kind"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "ADD CONSTRAINT ck_telegram_update_receipts_result_kind "
                "CHECK (result_kind IN ('captured', 'ignored'))"
            )
        )

    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.begin() as connection:
        for update_id, kind in (
            (40, "edit_mode_set"),
            (41, "edit_mode_cancelled"),
            (42, "record_edited"),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(bot_id, update_id, result_kind, trace_id, created_at) "
                    "VALUES (:bot, :upd, :kind, :trace, :ts)"
                ),
                {"bot": 1, "upd": update_id, "kind": kind, "trace": "e" * 32, "ts": TS},
            )
        stored = await connection.scalars(
            text(f"SELECT result_kind FROM {table} ORDER BY update_id")
        )
    assert set(stored.all()) == {
        "edit_mode_set",
        "edit_mode_cancelled",
        "record_edited",
    }


@pytest.mark.asyncio
async def test_initialize_reconciles_api_token_kinds_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    table = f'"{schema}".telegram_update_receipts'

    # Живая база до секции C: CHECK без api_token_*. Без реконсиляции первое же
    # нажатие «🔑 API» упало бы на старом ограничении.
    async with schema_engine.begin() as connection:
        await connection.execute(text(f"DELETE FROM {table}"))
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP CONSTRAINT ck_telegram_update_receipts_result_kind"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "ADD CONSTRAINT ck_telegram_update_receipts_result_kind "
                "CHECK (result_kind IN ('captured', 'ignored'))"
            )
        )

    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.begin() as connection:
        for update_id, kind in (
            (50, "api_tokens_listed"),
            (51, "api_token_created"),
            (52, "api_token_revoked"),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(bot_id, update_id, result_kind, trace_id, created_at) "
                    "VALUES (:bot, :upd, :kind, :trace, :ts)"
                ),
                {"bot": 1, "upd": update_id, "kind": kind, "trace": "f" * 32, "ts": TS},
            )
        stored = await connection.scalars(
            text(f"SELECT result_kind FROM {table} ORDER BY update_id")
        )
    assert set(stored.all()) == {
        "api_tokens_listed",
        "api_token_created",
        "api_token_revoked",
    }


@pytest.mark.asyncio
async def test_initialize_reconciles_digest_kinds_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    table = f'"{schema}".telegram_update_receipts'

    # Живая база до слайса «сводка»: CHECK без digest_menu_shown/digest_shown.
    async with schema_engine.begin() as connection:
        await connection.execute(text(f"DELETE FROM {table}"))
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP CONSTRAINT ck_telegram_update_receipts_result_kind"
            )
        )
        await connection.execute(
            text(
                f"ALTER TABLE {table} "
                "ADD CONSTRAINT ck_telegram_update_receipts_result_kind "
                "CHECK (result_kind IN ('captured', 'ignored'))"
            )
        )

    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.begin() as connection:
        for update_id, kind in (
            (30, "digest_menu_shown"),
            (31, "digest_shown"),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(bot_id, update_id, result_kind, trace_id, created_at) "
                    "VALUES (:bot, :upd, :kind, :trace, :ts)"
                ),
                {"bot": 1, "upd": update_id, "kind": kind, "trace": "d" * 32, "ts": TS},
            )
        stored = await connection.scalars(
            text(f"SELECT result_kind FROM {table} ORDER BY update_id")
        )
    assert set(stored.all()) == {"digest_menu_shown", "digest_shown"}

"""Реконсиляция живой БД до S3 «Правка записи»: колонка edited_at.

Живая прод-база (слайсы до S3) не имеет edited_at на типизированных таблицах.
create_all(checkfirst=True) существующие таблицы не трогает — доращивать
обязан initialize_schema, идемпотентно (ADD COLUMN IF NOT EXISTS).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import initialize_schema
from tests.identity.conftest import IsolatedDatabase

RECORD_TABLES = ("notes", "ideas", "decisions", "questions", "tasks")


async def _edited_at_columns(
    schema_engine: AsyncEngine, schema: str
) -> dict[str, bool]:
    async with schema_engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND column_name = 'edited_at'"
            ),
            {"schema": schema},
        )
        present = set(rows.scalars())
    return {table: table in present for table in RECORD_TABLES}


@pytest.mark.asyncio
async def test_initialize_adds_edited_at_to_all_record_tables_on_existing_db(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    # Имитируем живую базу до S3: колонки нет ни на одной таблице записей.
    async with schema_engine.begin() as connection:
        for table in RECORD_TABLES:
            await connection.execute(
                text(f'ALTER TABLE "{schema}".{table} DROP COLUMN IF EXISTS edited_at')
            )
    assert all(
        present is False
        for present in (await _edited_at_columns(schema_engine, schema)).values()
    )

    # Повторная инициализация на существующей схеме обязана дорастить колонку.
    await initialize_schema(schema_engine, schema)

    assert all((await _edited_at_columns(schema_engine, schema)).values())

    # Идемпотентность: второй init-db ничего не ломает.
    await initialize_schema(schema_engine, schema)
    assert all((await _edited_at_columns(schema_engine, schema)).values())

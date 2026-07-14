"""Реинициализация прототипной схемы на базе без расширения vector.

Воспроизводит сценарий активации Слайса 2: reset-db выполняется на базе,
где `CREATE EXTENSION vector` ещё ни разу не запускался (живой прототип,
поднятый до перехода на pgvector-образ). Сброс обязан сам установить
расширение, иначе создание semantic_documents падает на типе vector.
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from second_brain.bootstrap.schema import reset_prototype_schema

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_reset_prototype_schema_installs_vector_extension() -> None:
    admin_url = os.environ["TEST_SCHEMA_DATABASE_URL"]
    probe_database = f"ext_probe_{uuid.uuid4().hex}"

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{probe_database}"'))
        probe_url = admin_url.rsplit("/", 1)[0] + f"/{probe_database}"
        probe_engine = create_async_engine(probe_url)
        try:
            await reset_prototype_schema(probe_engine, confirm=True)

            async with probe_engine.connect() as connection:
                extension = await connection.execute(
                    text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                )
                assert extension.scalar_one_or_none() == "vector"
                table = await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables"
                        " WHERE tablename = 'semantic_documents'"
                    )
                )
                assert table.scalar_one_or_none() == "semantic_documents"
        finally:
            await probe_engine.dispose()
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{probe_database}" WITH (FORCE)')
            )
        await admin_engine.dispose()

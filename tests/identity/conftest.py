import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.schema import initialize_schema


@dataclass(frozen=True)
class IsolatedDatabase:
    database_url: str
    schema: str


def require_test_database_url() -> str:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if not test_database_url:
        raise RuntimeError(
            "TEST_DATABASE_URL must be set for PostgreSQL integration tests"
        )
    if test_database_url == os.environ.get("DATABASE_URL"):
        raise RuntimeError("TEST_DATABASE_URL must differ from DATABASE_URL")
    return test_database_url


@pytest_asyncio.fixture(scope="session")
async def isolated_database() -> AsyncIterator[IsolatedDatabase]:
    database_url = require_test_database_url()
    schema = f"test_identity_{uuid4().hex}"
    engine = create_database_engine(database_url)
    schema_engine = engine.execution_options(schema_translate_map={None: schema})

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        await initialize_schema(schema_engine)
        yield IsolatedDatabase(database_url=database_url, schema=schema)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture
async def engine(isolated_database: IsolatedDatabase) -> AsyncIterator[AsyncEngine]:
    database_engine = create_database_engine(isolated_database.database_url)
    schema_engine = database_engine.execution_options(
        schema_translate_map={None: isolated_database.schema}
    )

    try:
        yield schema_engine
    finally:
        await database_engine.dispose()


@pytest_asyncio.fixture
async def session(
    engine: AsyncEngine, isolated_database: IsolatedDatabase
) -> AsyncIterator[AsyncSession]:
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        await session.execute(text(f'SET search_path TO "{isolated_database.schema}"'))
        try:
            yield session
        finally:
            await session.rollback()

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import initialize_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
    create_session_factory,
)


@dataclass(frozen=True)
class IsolatedDatabase:
    database_url: str
    schema_database_url: str
    schema: str


def require_test_database_url() -> str:
    return require_test_database_urls()[0]


def require_test_database_urls() -> tuple[str, str]:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if not test_database_url:
        raise RuntimeError(
            "TEST_DATABASE_URL must be set for PostgreSQL integration tests"
        )
    test_schema_database_url = os.environ.get("TEST_SCHEMA_DATABASE_URL")
    if not test_schema_database_url:
        raise RuntimeError(
            "TEST_SCHEMA_DATABASE_URL must be set for PostgreSQL integration tests"
        )
    if test_database_url == test_schema_database_url:
        raise RuntimeError(
            "TEST_DATABASE_URL must differ from TEST_SCHEMA_DATABASE_URL"
        )
    if test_database_url == os.environ.get("DATABASE_URL"):
        raise RuntimeError("TEST_DATABASE_URL must differ from DATABASE_URL")
    if test_schema_database_url == os.environ.get("SCHEMA_DATABASE_URL"):
        raise RuntimeError(
            "TEST_SCHEMA_DATABASE_URL must differ from SCHEMA_DATABASE_URL"
        )
    return test_database_url, test_schema_database_url


@pytest_asyncio.fixture(scope="session")
async def isolated_database() -> AsyncIterator[IsolatedDatabase]:
    database_url, schema_database_url = require_test_database_urls()
    schema = f"test_identity_{uuid4().hex}"
    engine = create_database_engine(schema_database_url)
    schema_engine = engine.execution_options(schema_translate_map={None: schema})

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        await initialize_schema(schema_engine, schema)
        yield IsolatedDatabase(
            database_url=database_url,
            schema_database_url=schema_database_url,
            schema=schema,
        )
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
async def schema_engine(
    isolated_database: IsolatedDatabase,
) -> AsyncIterator[AsyncEngine]:
    database_engine = create_database_engine(isolated_database.schema_database_url)
    translated_engine = database_engine.execution_options(
        schema_translate_map={None: isolated_database.schema}
    )

    try:
        yield translated_engine
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

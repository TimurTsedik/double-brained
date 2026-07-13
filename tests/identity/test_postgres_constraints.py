from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    initialize_identity_schema,
    reset_identity_prototype_schema,
)
from tests.identity import conftest as identity_conftest
from tests.identity.conftest import IsolatedDatabase, require_test_database_url

TIMESTAMP = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


def test_requires_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL must be set"):
        require_test_database_url()


def test_rejects_test_database_url_equal_to_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://second_brain@127.0.0.1:5432/second_brain"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TEST_SCHEMA_DATABASE_URL",
        "postgresql+asyncpg://second_brain@127.0.0.1:5432/second_brain_test",
    )

    with pytest.raises(RuntimeError, match="must differ"):
        require_test_database_url()


def test_rejects_test_application_url_equal_to_test_owner_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://second_brain@127.0.0.1:5432/second_brain"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "SCHEMA_DATABASE_URL",
        "postgresql+asyncpg://second_brain@127.0.0.1:5432/second_brain",
    )
    test_database_url = "postgresql+asyncpg://second_brain@127.0.0.1:5432/test"
    monkeypatch.setenv("TEST_DATABASE_URL", test_database_url)
    monkeypatch.setenv("TEST_SCHEMA_DATABASE_URL", test_database_url)

    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL must differ"):
        require_test_database_url()


@pytest.mark.asyncio
async def test_initialize_schema_creates_identity_tables(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    )

    assert set(result.scalars()) == {
        "capture_events",
        "enrollment_attempts",
        "enrollment_invites",
        "telegram_identities",
        "telegram_update_receipts",
        "user_spaces",
        "users",
    }


@pytest.mark.asyncio
async def test_reset_requires_confirmation_without_mutating_schema(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="confirmation"):
        await reset_prototype_schema(engine, confirm=False)

    result = await session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    )
    assert set(result.scalars()) == {
        "capture_events",
        "enrollment_attempts",
        "enrollment_invites",
        "telegram_identities",
        "telegram_update_receipts",
        "user_spaces",
        "users",
    }


@pytest.mark.asyncio
async def test_identity_schema_lifecycle_excludes_capture_events(
    isolated_database: IsolatedDatabase,
) -> None:
    schema = f"test_identity_only_{uuid4().hex}"
    database_engine = create_database_engine(isolated_database.schema_database_url)
    schema_engine = database_engine.execution_options(
        schema_translate_map={None: schema}
    )
    try:
        async with database_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        await initialize_identity_schema(schema_engine, schema)
        await reset_identity_prototype_schema(
            schema_engine, confirm=True, schema_name=schema
        )

        async with database_engine.connect() as connection:
            result = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                {"schema": schema},
            )
        assert set(result.scalars()) == {
            "enrollment_attempts",
            "enrollment_invites",
            "telegram_identities",
            "telegram_update_receipts",
            "user_spaces",
            "users",
        }
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await database_engine.dispose()


@pytest.mark.asyncio
async def test_isolated_database_drops_schema_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = require_test_database_url()
    schema_id = UUID(int=1)
    schema = f"test_identity_{schema_id.hex}"
    monkeypatch.setattr(identity_conftest, "uuid4", lambda: schema_id)

    async def fail_initialization(_engine: AsyncEngine, _schema_name: str) -> None:
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(identity_conftest, "initialize_schema", fail_initialization)
    generator = identity_conftest.isolated_database.__wrapped__()

    verification_engine = create_database_engine(database_url)
    try:
        async with verification_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

        with pytest.raises(RuntimeError, match="initialization failed"):
            await anext(generator)

        async with verification_engine.connect() as connection:
            schema_is_absent = await connection.scalar(
                text("SELECT to_regnamespace(:schema) IS NULL"), {"schema": schema}
            )
        assert schema_is_absent is True
    finally:
        async with verification_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await verification_engine.dispose()


async def create_user(session: AsyncSession) -> User:
    user = User(
        id=uuid4(),
        role="admin",
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_postgres_requires_caller_supplied_timestamps(
    session: AsyncSession,
) -> None:
    session.add(User(id=uuid4(), role="admin"))

    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_stores_the_exact_caller_supplied_timestamps(
    session: AsyncSession,
) -> None:
    timestamp = datetime(2026, 7, 12, 12, 34, 56, tzinfo=UTC)
    user = User(
        id=uuid4(),
        role="admin",
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    assert user.created_at == timestamp
    assert user.updated_at == timestamp


@pytest.mark.asyncio
async def test_postgres_rejects_a_non_admin_user_role(session: AsyncSession) -> None:
    session.add(
        User(
            id=uuid4(),
            role="user",
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_rejects_a_user_space_outside_asia_jerusalem(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    session.add(
        UserSpace(
            id=uuid4(),
            owner_user_id=user.id,
            timezone="UTC",
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_rejects_a_user_space_for_an_unknown_owner(
    session: AsyncSession,
) -> None:
    session.add(
        UserSpace(
            id=uuid4(),
            owner_user_id=uuid4(),
            timezone="Asia/Jerusalem",
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_rejects_a_telegram_identity_for_an_unknown_user(
    session: AsyncSession,
) -> None:
    session.add(
        TelegramIdentity(
            id=uuid4(),
            telegram_user_id=4,
            user_id=uuid4(),
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_rejects_a_second_user_space_for_one_owner(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    session.add(
        UserSpace(
            id=uuid4(),
            owner_user_id=user.id,
            timezone="Asia/Jerusalem",
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )
    await session.commit()

    session.add(
        UserSpace(
            id=uuid4(),
            owner_user_id=user.id,
            timezone="Asia/Jerusalem",
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_rejects_two_active_identities_for_one_user(
    session: AsyncSession,
) -> None:
    user = await create_user(session)
    session.add(
        TelegramIdentity(
            id=uuid4(),
            telegram_user_id=1,
            user_id=user.id,
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )
    await session.commit()

    session.add(
        TelegramIdentity(
            id=uuid4(),
            telegram_user_id=2,
            user_id=user.id,
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_postgres_rejects_one_active_telegram_id_for_two_users(
    session: AsyncSession,
) -> None:
    first_user = await create_user(session)
    second_user = await create_user(session)
    session.add(
        TelegramIdentity(
            id=uuid4(),
            telegram_user_id=3,
            user_id=first_user.id,
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )
    await session.commit()

    session.add(
        TelegramIdentity(
            id=uuid4(),
            telegram_user_id=3,
            user_id=second_user.id,
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()

"""Хранение языка на user_spaces: колонка, CHECK, чтение/запись, резолверы.

Task 2 плана локализации. Колонка language VARCHAR(2) NULLABLE (NULL = «ещё не
выбран» → эффективный RU), CHECK IN ('ru','en'), КОЛОНОЧНЫЙ грант на (language,
updated_at), запись с owner-предикатом, worker/gateway резолверы locale.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.shared.i18n import Locale
from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresLocaleResolver,
    PostgresWorkerIdentityRepository,
    read_language_by_telegram_user,
    read_user_space_language,
    set_user_space_language,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    initialize_identity_schema,
)
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import IsolatedDatabase

TS = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


async def _seed_enrolled_user(
    session: AsyncSession,
    *,
    telegram_user_id: int = 7_000_001,
    language: str | None = None,
) -> tuple[UUID, UUID]:
    user = User(id=uuid4(), role="member", created_at=TS, updated_at=TS)
    session.add(user)
    await session.flush()
    user_space = UserSpace(
        id=uuid4(),
        owner_user_id=user.id,
        timezone="Asia/Jerusalem",
        language=language,
        created_at=TS,
        updated_at=TS,
    )
    session.add_all(
        [
            user_space,
            TelegramIdentity(
                id=uuid4(),
                telegram_user_id=telegram_user_id,
                user_id=user.id,
                created_at=TS,
                updated_at=TS,
            ),
        ]
    )
    await session.commit()
    return user.id, user_space.id


@pytest.mark.asyncio
async def test_initialize_adds_language_column_on_existing_db(
    isolated_database: IsolatedDatabase,
) -> None:
    # Собственная схема: init переприменяет грант ко ВСЕЙ схеме, поэтому нельзя
    # мутировать общую session-scoped схему (иначе снимутся гранты других тестов).
    schema = f"test_identity_language_{uuid4().hex}"
    database_engine = create_database_engine(isolated_database.schema_database_url)
    schema_engine = database_engine.execution_options(
        schema_translate_map={None: schema}
    )
    table = f'"{schema}".user_spaces'
    try:
        async with database_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await initialize_identity_schema(schema_engine, schema)

        # Имитируем живую базу слайсов 1–3: колонки language ещё нет.
        async with schema_engine.begin() as connection:
            await connection.execute(text(f"ALTER TABLE {table} DROP COLUMN language"))

        # Повторная инициализация обязана идемпотентно добавить колонку.
        await initialize_identity_schema(schema_engine, schema)
        await initialize_identity_schema(schema_engine, schema)

        async with schema_engine.connect() as connection:
            column_type = await connection.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'user_spaces' "
                    "AND column_name = 'language'"
                ),
                {"schema": schema},
            )
        assert column_type == "character varying"
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await database_engine.dispose()


@pytest.mark.asyncio
async def test_language_check_rejects_unknown_allows_known_and_null(
    session: AsyncSession,
) -> None:
    user_id, user_space_id = await _seed_enrolled_user(
        session, telegram_user_id=7_000_010
    )

    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE user_spaces SET language = 'de' WHERE id = :id"),
            {"id": user_space_id},
        )
        await session.flush()
    await session.rollback()

    ru_owner, ru_space = await _seed_enrolled_user(
        session, telegram_user_id=7_000_011, language="ru"
    )
    en_owner, en_space = await _seed_enrolled_user(
        session, telegram_user_id=7_000_012, language="en"
    )
    null_owner, null_space = await _seed_enrolled_user(
        session, telegram_user_id=7_000_013
    )
    assert await read_user_space_language(session, ru_space, ru_owner) == "ru"
    assert await read_user_space_language(session, en_space, en_owner) == "en"
    assert await read_user_space_language(session, null_space, null_owner) is None
    # Owner-предикат чтения: чужой владелец не видит язык этого space.
    assert await read_user_space_language(session, ru_space, uuid4()) is None


@pytest.mark.asyncio
async def test_set_language_writes_only_own_space_and_bumps_updated_at(
    session: AsyncSession,
) -> None:
    owner_id, user_space_id = await _seed_enrolled_user(
        session, telegram_user_id=7_000_020
    )
    assert await read_user_space_language(session, user_space_id, owner_id) is None

    changed = await set_user_space_language(
        session, user_space_id, owner_id, "en", datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    )
    await session.commit()
    assert changed is True
    assert await read_user_space_language(session, user_space_id, owner_id) == "en"

    row = (
        await session.execute(
            text(
                "SELECT language, updated_at, timezone, owner_user_id, is_active "
                "FROM user_spaces WHERE id = :id"
            ),
            {"id": user_space_id},
        )
    ).one()
    assert row.language == "en"
    assert row.updated_at == datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    assert row.timezone == "Asia/Jerusalem"
    assert row.owner_user_id == owner_id
    assert row.is_active is True

    # Чужой владелец не может сменить язык (owner-предикат).
    stranger = uuid4()
    changed_by_stranger = await set_user_space_language(
        session, user_space_id, stranger, "ru", datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    )
    await session.commit()
    assert changed_by_stranger is False
    assert await read_user_space_language(session, user_space_id, owner_id) == "en"


@pytest.mark.asyncio
async def test_worker_resolve_locale_maps_null_and_en(
    engine: AsyncEngine,
    isolated_database: IsolatedDatabase,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await session.execute(
            text(f'SET search_path TO "{isolated_database.schema}", public')
        )
        null_owner, null_space = await _seed_enrolled_user(
            session, telegram_user_id=7_000_030
        )
        en_owner, en_space = await _seed_enrolled_user(
            session, telegram_user_id=7_000_031, language="en"
        )

    repository = PostgresWorkerIdentityRepository(session_factory)
    assert (
        await repository.resolve_locale(
            AccessContext(user_id=null_owner, user_space_id=null_space)
        )
        is Locale.RU
    )
    assert (
        await repository.resolve_locale(
            AccessContext(user_id=en_owner, user_space_id=en_space)
        )
        is Locale.EN
    )


@pytest.mark.asyncio
async def test_gateway_locale_resolver_maps_by_telegram_user(
    engine: AsyncEngine,
    isolated_database: IsolatedDatabase,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await session.execute(
            text(f'SET search_path TO "{isolated_database.schema}", public')
        )
        await _seed_enrolled_user(session, telegram_user_id=7_000_040)
        await _seed_enrolled_user(session, telegram_user_id=7_000_041, language="en")

    resolver = PostgresLocaleResolver(session_factory)
    assert await resolver.resolve_for_telegram_user(7_000_040) is Locale.RU
    assert await resolver.resolve_for_telegram_user(7_000_041) is Locale.EN
    assert await resolver.resolve_for_telegram_user(8_000_000) is Locale.RU

    async with session_factory() as session:
        await session.execute(
            text(f'SET search_path TO "{isolated_database.schema}", public')
        )
        assert await read_language_by_telegram_user(session, 7_000_041) == "en"
        assert await read_language_by_telegram_user(session, 7_000_040) is None
        assert await read_language_by_telegram_user(session, 8_000_000) is None

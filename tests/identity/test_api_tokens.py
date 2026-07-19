"""Хранилище и жизненный цикл токенов доступа к API (эпик API-1, секция C).

Проверяется то, ради чего таблица заведена именно такой: строка читается ВНЕ
пространства (RLS нет — по токену мы только и узнаём, кто пришёл), перец
отдельный от инвайтов (ротация одного не разлогинивает другой), отзыв — это
пометка, а не удаление, и запись last_used_at не происходит на КАЖДЫЙ запрос.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Update, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    ApiToken,
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresApiTokenRepository,
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    initialize_identity_schema,
)
from second_brain.slices.identity.application.api_tokens import (
    ApiTokenLifecycle,
    AuthenticateApiToken,
)
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
PEPPER = b"api-token-pepper"
PEPPER_KEY_ID = "api-key-v1"
THROTTLE = timedelta(minutes=5)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture(autouse=True)
async def reset_api_token_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


async def seed_user(
    schema_engine: AsyncEngine,
    *,
    telegram_user_id: int = 42,
    role: str = "admin",
    user_active: bool = True,
    space_active: bool = True,
) -> AccessContext:
    user_id = uuid4()
    space_id = uuid4()
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            session.add(
                User(
                    id=user_id,
                    role=role,
                    is_active=user_active,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add_all(
                [
                    UserSpace(
                        id=space_id,
                        owner_user_id=user_id,
                        timezone="Asia/Jerusalem",
                        language="ru",
                        is_active=space_active,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    TelegramIdentity(
                        id=uuid4(),
                        telegram_user_id=telegram_user_id,
                        user_id=user_id,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
    return AccessContext(user_id=user_id, user_space_id=space_id)


def lifecycle() -> ApiTokenLifecycle:
    return ApiTokenLifecycle(pepper=PEPPER, pepper_key_id=PEPPER_KEY_ID)


def authenticator(engine: AsyncEngine, clock: FixedClock) -> AuthenticateApiToken:
    return AuthenticateApiToken(
        repository=PostgresApiTokenRepository(create_session_factory(engine)),
        clock=clock,
        pepper=PEPPER,
        pepper_key_id=PEPPER_KEY_ID,
        last_used_throttle=THROTTLE,
    )


class PausedBeforeMarkSession(AsyncSession):
    """Сессия, которая замирает перед записью отметки последнего использования.

    Нужна ровно для одного: задать порядок событий вручную. Строка уже
    прочитана, транзакция ещё открыта — и в этот промежуток соседняя транзакция
    успевает сделать своё дело и закоммититься.
    """

    def __init__(
        self,
        *args: Any,
        reached: asyncio.Event,
        resume: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._reached = reached
        self._resume = resume

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update):
            self._reached.set()
            await self._resume.wait()
        return await super().execute(statement, *args, **kwargs)


def paused_authenticator(
    engine: AsyncEngine,
    clock: FixedClock,
    reached: asyncio.Event,
    resume: asyncio.Event,
) -> AuthenticateApiToken:
    return AuthenticateApiToken(
        repository=PostgresApiTokenRepository(
            async_sessionmaker(
                engine,
                expire_on_commit=False,
                class_=PausedBeforeMarkSession,
                reached=reached,
                resume=resume,
            )
        ),
        clock=clock,
        pepper=PEPPER,
        pepper_key_id=PEPPER_KEY_ID,
        last_used_throttle=THROTTLE,
    )


# ---------------------------------------------------------------------------
# схема: таблица есть, RLS на ней НЕТ (и это осознанно)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_tokens_table_has_no_row_level_security(
    session: AsyncSession,
) -> None:
    # Проверка токена идёт ДО того, как известно пространство: строка обязана
    # читаться вне scope — как enrollment_invites.
    for table_name in ("api_tokens", "enrollment_invites"):
        enabled = await session.scalar(
            text(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE oid = to_regclass(:table_name)"
            ),
            {"table_name": table_name},
        )
        assert enabled is False, table_name


@pytest.mark.asyncio
async def test_initialize_grows_api_tokens_on_a_live_database(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    # Живая база секции B таблицы не знает: init-db обязан её доростить.
    schema = isolated_database.schema
    async with schema_engine.begin() as connection:
        await connection.execute(text(f'DROP TABLE "{schema}".api_tokens'))

    await initialize_identity_schema(schema_engine, schema)

    async with schema_engine.connect() as connection:
        exists = await connection.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"{schema}.api_tokens"},
        )
    assert exists is True


# ---------------------------------------------------------------------------
# выдать / перечислить / отозвать
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_stores_only_the_hash_and_returns_the_secret_once(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    access = await seed_user(schema_engine)

    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            issued = await lifecycle().issue(
                access, PostgresUpdateTransaction(db_session), NOW
            )

    stored = await session.scalar(select(ApiToken))
    assert stored is not None
    assert issued.secret
    # Секрет в базе не лежит ни в каком виде, кроме хэша.
    assert issued.secret.encode() != stored.token_hash
    assert stored.pepper_key_id == PEPPER_KEY_ID
    assert stored.revoked_at is None
    assert stored.last_used_at is None
    assert stored.label == issued.view.label
    # Плейнтекст не должен попадать в repr.
    assert issued.secret not in repr(issued)


@pytest.mark.asyncio
async def test_listing_shows_labels_state_and_never_the_secret(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_user(schema_engine)

    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            transaction = PostgresUpdateTransaction(db_session)
            first = await lifecycle().issue(access, transaction, NOW)
            second = await lifecycle().issue(
                access, transaction, NOW + timedelta(minutes=1)
            )
            listed = await lifecycle().list_tokens(access, transaction)

    assert [view.label for view in listed] == [first.view.label, second.view.label]
    assert first.view.label != second.view.label
    assert all(view.revoked_at is None for view in listed)
    assert all(view.last_used_at is None for view in listed)
    assert not any(secret in repr(listed) for secret in (first.secret, second.secret))


@pytest.mark.asyncio
async def test_revoke_marks_the_row_and_is_idempotent(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    access = await seed_user(schema_engine)
    later = NOW + timedelta(hours=1)

    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            transaction = PostgresUpdateTransaction(db_session)
            issued = await lifecycle().issue(access, transaction, NOW)
            first = await lifecycle().revoke(access, transaction, issued.view.id, NOW)
            second = await lifecycle().revoke(
                access, transaction, issued.view.id, later
            )

    stored = await session.scalar(select(ApiToken))
    assert (first, second) == (True, True)
    assert stored is not None
    # История остаётся, время отзыва не переписывается повторным нажатием.
    assert stored.revoked_at == NOW


@pytest.mark.asyncio
async def test_revoking_a_foreign_token_changes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    owner = await seed_user(schema_engine, telegram_user_id=1)
    stranger = await seed_user(schema_engine, telegram_user_id=2, role="member")

    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            transaction = PostgresUpdateTransaction(db_session)
            issued = await lifecycle().issue(owner, transaction, NOW)
            revoked = await lifecycle().revoke(
                stranger, transaction, issued.view.id, NOW
            )
            visible = await lifecycle().list_tokens(stranger, transaction)

    stored = await session.scalar(select(ApiToken))
    assert revoked is False
    assert visible == ()
    assert stored is not None and stored.revoked_at is None


# ---------------------------------------------------------------------------
# проверка предъявленного секрета (для следующего слайса)
# ---------------------------------------------------------------------------


async def issue_for(engine: AsyncEngine, access: AccessContext) -> str:
    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            issued = await lifecycle().issue(
                access, PostgresUpdateTransaction(db_session), NOW
            )
    return issued.secret


@pytest.mark.asyncio
async def test_authentication_resolves_the_owner_of_a_live_token(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_user(schema_engine)
    secret = await issue_for(engine, access)

    principal = await authenticator(engine, FixedClock()).execute(secret)

    assert principal is not None
    assert principal.access_context == access


@pytest.mark.asyncio
async def test_authentication_rejects_a_revoked_or_unknown_secret(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_user(schema_engine)
    secret = await issue_for(engine, access)
    live = await authenticator(engine, FixedClock()).execute(secret)
    assert live is not None

    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            await lifecycle().revoke(
                access, PostgresUpdateTransaction(db_session), live.token_id, NOW
            )

    assert await authenticator(engine, FixedClock()).execute(secret) is None
    assert await authenticator(engine, FixedClock()).execute("not-a-token") is None


@pytest.mark.asyncio
async def test_authentication_rejects_a_token_of_a_deactivated_user(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_user(schema_engine, user_active=False)
    secret = await issue_for(engine, access)

    assert await authenticator(engine, FixedClock()).execute(secret) is None


@pytest.mark.asyncio
async def test_authentication_rejects_a_secret_peppered_with_another_key(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Ротация перца API не должна принимать старые токены — и наоборот, ротация
    # перца инвайтов их не трогает (у токенов свой pepper_key_id).
    access = await seed_user(schema_engine)
    secret = await issue_for(engine, access)
    rotated = AuthenticateApiToken(
        repository=PostgresApiTokenRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=PEPPER,
        pepper_key_id="api-key-v2",
        last_used_throttle=THROTTLE,
    )

    assert await rotated.execute(secret) is None


@pytest.mark.asyncio
async def test_last_used_is_written_once_per_throttle_window(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Запись на КАЖДЫЙ запрос API — это запись на каждое чтение; отметка
    # обновляется не чаще раза в окно.
    access = await seed_user(schema_engine)
    secret = await issue_for(engine, access)
    clock = FixedClock()
    auth = authenticator(engine, clock)

    await auth.execute(secret)
    first = await session.scalar(select(ApiToken.last_used_at))

    clock.value = NOW + timedelta(minutes=1)
    await auth.execute(secret)
    within_window = await session.scalar(select(ApiToken.last_used_at))

    clock.value = NOW + timedelta(minutes=6)
    await auth.execute(secret)
    after_window = await session.scalar(select(ApiToken.last_used_at))

    assert first == NOW
    assert within_window == NOW
    assert after_window == NOW + timedelta(minutes=6)


@pytest.mark.asyncio
async def test_last_used_never_moves_back_when_an_older_mark_is_written_last(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Два запроса одним токеном видят одну и ту же пустую отметку. Тот, у кого
    # момент РАНЬШЕ, доходит до записи последним — и не должен затереть более
    # позднюю: поле отвечает на вопрос «когда ключом пользовались в последний
    # раз», и уехавшая назад отметка отвечает на него неправду.
    access = await seed_user(schema_engine)
    secret = await issue_for(engine, access)
    later = NOW + timedelta(hours=1)
    reached, resume = asyncio.Event(), asyncio.Event()
    earlier_request = asyncio.create_task(
        paused_authenticator(engine, FixedClock(NOW), reached, resume).execute(secret)
    )

    await reached.wait()
    assert await authenticator(engine, FixedClock(later)).execute(secret) is not None
    resume.set()
    assert await earlier_request is not None

    assert await session.scalar(select(ApiToken.last_used_at)) == later


@pytest.mark.asyncio
async def test_a_token_revoked_mid_request_gets_no_last_used_mark(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Отзыв успевает закоммититься между чтением строки и записью отметки:
    # отозванный токен отметку не получает.
    access = await seed_user(schema_engine)
    secret = await issue_for(engine, access)
    reached, resume = asyncio.Event(), asyncio.Event()
    request = asyncio.create_task(
        paused_authenticator(engine, FixedClock(), reached, resume).execute(secret)
    )

    await reached.wait()
    async with create_session_factory(engine)() as db_session:
        async with db_session.begin():
            token_id = await db_session.scalar(select(ApiToken.id))
            assert token_id is not None
            revoked = await lifecycle().revoke(
                access, PostgresUpdateTransaction(db_session), token_id, NOW
            )
    assert revoked is True
    resume.set()
    await request

    assert await session.scalar(select(ApiToken.last_used_at)) is None

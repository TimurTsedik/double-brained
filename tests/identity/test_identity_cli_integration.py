import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap import identity_cli
from second_brain.bootstrap.settings import Settings
from second_brain.slices.identity.adapters.persistence.models import EnrollmentInvite
from tests.identity.conftest import IsolatedDatabase


@pytest.mark.asyncio
async def test_missing_bot_username_does_not_create_a_pending_invite(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    class Session:
        async def close(self) -> None:
            return None

    class BotWithoutUsername:
        def __init__(self, _token: str) -> None:
            self.session = Session()

        async def get_me(self):
            return type("BotUser", (), {"username": None})()

    monkeypatch.setattr(identity_cli, "Bot", BotWithoutUsername)
    settings = Settings(
        database_url="postgresql+asyncpg://unused",
        schema_database_url="postgresql+asyncpg://unused-owner",
        telegram_bot_token="token",
        invite_token_pepper=b"pepper",
        invite_token_pepper_key_id="key-1",
    )
    pending_before = await session.scalar(
        select(func.count())
        .select_from(EnrollmentInvite)
        .where(EnrollmentInvite.status == "pending")
    )

    with pytest.raises(RuntimeError, match="username"):
        await identity_cli._print_invite(engine, settings)

    pending_count = await session.scalar(
        select(func.count())
        .select_from(EnrollmentInvite)
        .where(EnrollmentInvite.status == "pending")
    )
    assert pending_count == pending_before


@pytest.mark.asyncio
async def test_create_bootstrap_invite_rejects_owner_role_before_any_write(
    isolated_database: IsolatedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    class BotMustNotBeCalled:
        def __init__(self, _token: str) -> None:
            self.session = self

        async def get_me(self) -> None:
            raise AssertionError("Telegram must not be called for an owner role")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(identity_cli, "Bot", BotMustNotBeCalled)
    settings = Settings(
        database_url=isolated_database.schema_database_url,
        schema_database_url=isolated_database.schema_database_url,
        telegram_bot_token="token",
        invite_token_pepper=b"pepper",
        invite_token_pepper_key_id="key-1",
    )
    pending_before = await session.scalar(
        select(func.count()).select_from(EnrollmentInvite)
    )

    with pytest.raises(RuntimeError, match="second_brain_app"):
        await identity_cli.run(
            identity_cli.parse_args(["create-bootstrap-admin-invite"]), settings
        )

    pending_after = await session.scalar(
        select(func.count()).select_from(EnrollmentInvite)
    )
    assert pending_after == pending_before


@pytest.mark.asyncio
async def test_init_and_reset_keep_using_the_owner_connection(
    isolated_database: IsolatedDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized: list[AsyncEngine] = []
    reset: list[AsyncEngine] = []

    async def record_initialize(engine: AsyncEngine) -> None:
        initialized.append(engine)

    async def record_reset(engine: AsyncEngine, confirm: bool) -> None:
        assert confirm is True
        reset.append(engine)

    monkeypatch.setattr(identity_cli, "initialize_schema", record_initialize)
    monkeypatch.setattr(identity_cli, "reset_prototype_schema", record_reset)
    settings = Settings(
        database_url="postgresql+asyncpg://unused-app",
        schema_database_url=isolated_database.schema_database_url,
        telegram_bot_token="token",
        invite_token_pepper=b"pepper",
        invite_token_pepper_key_id="key-1",
    )

    await identity_cli.run(identity_cli.parse_args(["init-db"]), settings)
    await identity_cli.run(
        identity_cli.parse_args(["reset-db", "--confirm-prototype-reset"]), settings
    )

    assert len(initialized) == 1
    assert len(reset) == 1

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap import identity_cli
from second_brain.bootstrap.settings import Settings
from second_brain.slices.identity.adapters.persistence.models import EnrollmentInvite


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

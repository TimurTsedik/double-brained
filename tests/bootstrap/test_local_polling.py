import pytest

from second_brain.bootstrap import local_polling
from second_brain.bootstrap.settings import Settings


@pytest.mark.asyncio
async def test_local_polling_checks_database_role_before_telegram_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBot:
        def __init__(self, _token: str) -> None:
            self.session = self
            self.get_me_called = False

        async def get_me(self) -> None:
            self.get_me_called = True

        async def close(self) -> None:
            return None

    bot = FakeBot("unused")

    async def reject_privileged_role(_engine: object) -> None:
        raise RuntimeError("non-superuser PostgreSQL role required")

    monkeypatch.setattr(local_polling, "Bot", lambda _token: bot)
    monkeypatch.setattr(
        local_polling,
        "assert_non_privileged_application_role",
        reject_privileged_role,
        raising=False,
    )
    settings = Settings(
        database_url="postgresql+asyncpg://unused",
        schema_database_url="postgresql+asyncpg://unused-owner",
        telegram_bot_token="token",
        invite_token_pepper=b"pepper",
        invite_token_pepper_key_id="local-v1",
    )

    with pytest.raises(RuntimeError, match="non-superuser"):
        await local_polling.run_local_polling(settings)

    assert bot.get_me_called is False

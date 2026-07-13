import pytest

from second_brain.bootstrap.identity_cli import parse_args
from second_brain.bootstrap.settings import Settings


def test_settings_requires_all_identity_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("INVITE_TOKEN_PEPPER", raising=False)
    monkeypatch.delenv("INVITE_TOKEN_PEPPER_KEY_ID", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        Settings.from_environment()


def test_settings_keeps_secrets_out_of_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://database-secret@example")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper-secret")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "key-1")

    settings = Settings.from_environment()

    assert settings.invite_token_pepper == b"pepper-secret"
    assert "database-secret" not in repr(settings)
    assert "bot-secret" not in repr(settings)
    assert "pepper-secret" not in repr(settings)


def test_reset_command_requires_the_explicit_prototype_confirmation_flag() -> None:
    assert parse_args(["reset-db"]).confirm_prototype_reset is False
    assert parse_args(["reset-db", "--confirm-prototype-reset"]).confirm_prototype_reset


def test_cli_exposes_the_bootstrap_admin_invite_command() -> None:
    assert parse_args(["create-bootstrap-admin-invite"]).command == (
        "create-bootstrap-admin-invite"
    )

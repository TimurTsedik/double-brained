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


def test_settings_require_schema_owner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://second_brain_app@localhost/database"
    )
    monkeypatch.delenv("SCHEMA_DATABASE_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "local-v1")

    with pytest.raises(RuntimeError, match="SCHEMA_DATABASE_URL"):
        Settings.from_environment()


def test_settings_reject_application_url_equal_to_owner_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://second_brain@localhost/database"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SCHEMA_DATABASE_URL", database_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "local-v1")

    with pytest.raises(RuntimeError, match="DATABASE_URL must differ"):
        Settings.from_environment()


def test_settings_keeps_secrets_out_of_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://database-secret@example")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper-secret")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "key-1")
    monkeypatch.setenv(
        "SCHEMA_DATABASE_URL", "postgresql+asyncpg://schema-secret@example"
    )
    monkeypatch.setenv("VOICE_STORAGE_ROOT", "/private/voice-storage")
    monkeypatch.setenv("WHISPER_MODEL", "local-test-model")
    monkeypatch.setenv("OPEN_ROUTER_AI_KEY", "openrouter-secret")

    settings = Settings.from_environment()

    assert settings.invite_token_pepper == b"pepper-secret"
    assert "database-secret" not in repr(settings)
    assert "bot-secret" not in repr(settings)
    assert "pepper-secret" not in repr(settings)
    assert "schema-secret" not in repr(settings)
    assert "/private/voice-storage" not in repr(settings)
    assert "openrouter-secret" not in repr(settings)
    assert settings.voice_storage_root == "/private/voice-storage"
    assert settings.whisper_model == "local-test-model"
    assert settings.open_router_ai_key == "openrouter-secret"


def test_voice_settings_have_local_defaults_and_optional_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@example")
    monkeypatch.setenv("SCHEMA_DATABASE_URL", "postgresql+asyncpg://owner@example")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "key-1")
    monkeypatch.delenv("VOICE_STORAGE_ROOT", raising=False)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_AI_KEY", raising=False)

    settings = Settings.from_environment()

    assert settings.voice_storage_root == ".data/voice"
    assert settings.whisper_model == "small"
    assert settings.open_router_ai_key is None


def test_reset_command_requires_the_explicit_prototype_confirmation_flag() -> None:
    assert parse_args(["reset-db"]).confirm_prototype_reset is False
    assert parse_args(["reset-db", "--confirm-prototype-reset"]).confirm_prototype_reset


def test_cli_exposes_the_bootstrap_admin_invite_command() -> None:
    assert parse_args(["create-bootstrap-admin-invite"]).command == (
        "create-bootstrap-admin-invite"
    )

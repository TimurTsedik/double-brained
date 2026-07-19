import pytest

from second_brain.bootstrap.identity_cli import parse_args
from second_brain.bootstrap.settings import Settings
from tests.bootstrap.conftest import set_required_environment


def test_settings_requires_all_identity_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("INVITE_TOKEN_PEPPER", raising=False)
    monkeypatch.delenv("INVITE_TOKEN_PEPPER_KEY_ID", raising=False)
    monkeypatch.delenv("API_TOKEN_PEPPER", raising=False)
    monkeypatch.delenv("API_TOKEN_PEPPER_KEY_ID", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        Settings.from_environment()


def test_settings_require_schema_owner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Окружение полное, и не хватает РОВНО одной переменной — иначе тест ловил
    # бы жалобу на что-то другое и лишь по порядку чтения выглядел зелёным.
    set_required_environment(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://second_brain_app@localhost/database"
    )
    monkeypatch.delenv("SCHEMA_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="SCHEMA_DATABASE_URL"):
        Settings.from_environment()


def test_settings_reject_application_url_equal_to_owner_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://second_brain@localhost/database"
    set_required_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SCHEMA_DATABASE_URL", database_url)

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
    monkeypatch.setenv("API_TOKEN_PEPPER", "api-pepper-secret")
    monkeypatch.setenv("API_TOKEN_PEPPER_KEY_ID", "api-key-1")
    monkeypatch.setenv("VOICE_STORAGE_ROOT", "/private/voice-storage")
    monkeypatch.setenv("WHISPER_MODEL", "local-test-model")
    monkeypatch.setenv("OPEN_ROUTER_AI_KEY", "openrouter-secret")

    settings = Settings.from_environment()

    assert settings.invite_token_pepper == b"pepper-secret"
    # Перец API-токенов — отдельный секрет, и тоже вне repr.
    assert settings.api_token_pepper == b"api-pepper-secret"
    assert "api-pepper-secret" not in repr(settings)
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
    set_required_environment(monkeypatch)
    monkeypatch.delenv("VOICE_STORAGE_ROOT", raising=False)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_AI_KEY", raising=False)

    settings = Settings.from_environment()

    assert settings.voice_storage_root == ".data/voice"
    assert settings.whisper_model == "small"
    assert settings.open_router_ai_key is None


def test_settings_require_the_separate_api_token_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Перец API-токенов НЕ подменяется перцем приглашений: без своей переменной
    # приложение не стартует, а не тихо солит токены чужим секретом.
    set_required_environment(monkeypatch)
    monkeypatch.delenv("API_TOKEN_PEPPER", raising=False)

    with pytest.raises(RuntimeError, match="API_TOKEN_PEPPER"):
        Settings.from_environment()


def test_api_token_last_used_throttle_defaults_and_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.delenv("API_TOKEN_LAST_USED_THROTTLE_SECONDS", raising=False)
    assert Settings.from_environment().api_token_last_used_throttle_seconds == 300

    monkeypatch.setenv("API_TOKEN_LAST_USED_THROTTLE_SECONDS", "60")
    assert Settings.from_environment().api_token_last_used_throttle_seconds == 60


def test_panel_followup_defaults_to_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.delenv("PANEL_FOLLOWUP_SECONDS", raising=False)

    assert Settings.from_environment().panel_followup_seconds == 5


def test_panel_followup_reads_seconds_and_zero_means_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("PANEL_FOLLOWUP_SECONDS", "12")
    assert Settings.from_environment().panel_followup_seconds == 12

    monkeypatch.setenv("PANEL_FOLLOWUP_SECONDS", "0")
    assert Settings.from_environment().panel_followup_seconds == 0


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5"])
def test_panel_followup_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("PANEL_FOLLOWUP_SECONDS", raw)

    with pytest.raises(RuntimeError, match="PANEL_FOLLOWUP_SECONDS"):
        Settings.from_environment()


def test_reset_command_requires_the_explicit_prototype_confirmation_flag() -> None:
    assert parse_args(["reset-db"]).confirm_prototype_reset is False
    assert parse_args(["reset-db", "--confirm-prototype-reset"]).confirm_prototype_reset


def test_cli_exposes_the_bootstrap_admin_invite_command() -> None:
    assert parse_args(["create-bootstrap-admin-invite"]).command == (
        "create-bootstrap-admin-invite"
    )

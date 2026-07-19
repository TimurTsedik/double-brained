import pytest

from tests.identity.conftest import engine, isolated_database, schema_engine, session

__all__ = [
    "engine",
    "isolated_database",
    "schema_engine",
    "session",
    "set_required_environment",
]


def set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Минимально валидное окружение для Settings.from_environment().

    Набор обязательных переменных держим в ОДНОМ месте намеренно. Пока каждый
    тест собирал окружение сам, добавление новой обязательной переменной роняло
    разрозненные тесты не по их предмету: тест падал на настройке раньше, чем
    доходил до того, что проверяет, — и переставал что-либо доказывать.

    Значения фейковые: по этим адресам никто не ходит и в сеть тесты не лезут.
    Тест, которому важно конкретное значение (свой токен бота, своя изолированная
    база), вызывает helper и переопределяет нужное своим setenv следом.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@example")
    monkeypatch.setenv("SCHEMA_DATABASE_URL", "postgresql+asyncpg://owner@example")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "key-1")
    monkeypatch.setenv("API_TOKEN_PEPPER", "api-pepper")
    monkeypatch.setenv("API_TOKEN_PEPPER_KEY_ID", "api-key-1")

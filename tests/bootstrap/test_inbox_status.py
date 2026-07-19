"""Консольный статус webhook-очереди (эпик API-1, B4): что видит оператор.

Инструмент runbook'а: показывает глубину INBOX, возраст головы и мнение самого
Telegram о доставке. Здесь пришпилены три вещи, ради которых слайс и делался:
человекочитаемый вывод, код возврата для внешнего планировщика и молчание про
токен бота — включая путь «Telegram недоступен», где текст исключения aiogram
может содержать URL запроса вместе с токеном.
"""

from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import WebhookInfo

from second_brain.bootstrap import inbox_status
from second_brain.bootstrap.inbox_status import (
    EXIT_OK,
    EXIT_UNHEALTHY,
    EXIT_UNKNOWN,
    REDACTED,
    WebhookView,
    main,
    read_webhook_view,
    render_report,
    run,
)
from second_brain.bootstrap.settings import Settings
from second_brain.slices.identity.adapters.persistence.inbox import TelegramInboxHealth

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
BOT_ID = 700
HEAD_AGE_ALERT_SECONDS = 300
WEBHOOK_ERROR_WINDOW_SECONDS = 3600
BOT_TOKEN = "700:AA-secret-bot-token"
WEBHOOK_SECRET = "header-secret-value"

EMPTY_QUEUE = TelegramInboxHealth(
    pending_count=0, failed_count=0, head_age_seconds=None
)
QUIET_WEBHOOK = WebhookView(
    url="https://yousaid.example/telegram/webhook",
    pending_update_count=0,
    max_connections=1,
    last_error_at=None,
    last_error_message=None,
)


def report(
    *,
    health: TelegramInboxHealth = EMPTY_QUEUE,
    webhook: WebhookView | None = QUIET_WEBHOOK,
    webhook_error: str | None = None,
) -> tuple[str, int]:
    return render_report(
        NOW,
        bot_id=BOT_ID,
        health=health,
        webhook=webhook,
        webhook_error=webhook_error,
        head_age_alert_seconds=HEAD_AGE_ALERT_SECONDS,
        webhook_error_window_seconds=WEBHOOK_ERROR_WINDOW_SECONDS,
        secrets=(BOT_TOKEN, WEBHOOK_SECRET),
    )


class FakeWebhookSource:
    def __init__(self, info: WebhookInfo) -> None:
        self._info = info

    async def get_webhook_info(self) -> WebhookInfo:
        return self._info


class UnreachableWebhookSource:
    async def get_webhook_info(self) -> WebhookInfo:
        # Именно так течёт токен: aiogram/aiohttp кладут URL запроса в текст.
        raise RuntimeError(
            f"cannot connect to https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
        )


def test_empty_queue_and_quiet_telegram_report_ok() -> None:
    text, code = report()

    assert code == EXIT_OK
    assert "OK:" in text
    assert "pending" in text


def test_stuck_head_over_the_threshold_is_unhealthy() -> None:
    text, code = report(
        health=TelegramInboxHealth(
            pending_count=47, failed_count=0, head_age_seconds=912.0
        )
    )

    assert code == EXIT_UNHEALTHY
    assert "47" in text
    assert "912" in text
    assert str(HEAD_AGE_ALERT_SECONDS) in text


def test_head_below_the_threshold_stays_ok() -> None:
    # Порог — единственное, что отделяет «работает» от «встала»: очередь с
    # молодой головой не должна будить планировщик.
    text, code = report(
        health=TelegramInboxHealth(
            pending_count=3, failed_count=0, head_age_seconds=12.0
        )
    )

    assert code == EXIT_OK
    assert "3" in text


def test_permanently_failed_rows_are_unhealthy() -> None:
    text, code = report(
        health=TelegramInboxHealth(
            pending_count=0, failed_count=2, head_age_seconds=None
        )
    )

    assert code == EXIT_UNHEALTHY
    assert "failed" in text


def test_fresh_telegram_delivery_error_is_unhealthy_and_shown() -> None:
    text, code = report(
        webhook=WebhookView(
            url="https://yousaid.example/telegram/webhook",
            pending_update_count=120,
            max_connections=1,
            last_error_at=NOW - timedelta(minutes=4),
            last_error_message="Wrong response from the webhook: 502 Bad Gateway",
        )
    )

    assert code == EXIT_UNHEALTHY
    assert "502 Bad Gateway" in text
    assert "120" in text


def test_stale_telegram_delivery_error_is_history_not_an_alarm() -> None:
    # Telegram НЕ сбрасывает last_error после удачной доставки (это делает
    # только setWebhook), поэтому старая ошибка не должна держать команду
    # вечно красной — иначе код возврата бесполезен для планировщика.
    text, code = report(
        webhook=WebhookView(
            url="https://yousaid.example/telegram/webhook",
            pending_update_count=0,
            max_connections=1,
            last_error_at=NOW - timedelta(days=3),
            last_error_message="Read timeout expired",
        )
    )

    assert code == EXIT_OK
    assert "Read timeout expired" in text


def test_unreachable_telegram_still_shows_the_database_numbers() -> None:
    text, code = report(webhook=None, webhook_error="TelegramNetworkError")

    assert code == EXIT_UNKNOWN
    assert "pending" in text
    assert "TelegramNetworkError" in text


def test_broken_queue_outweighs_an_unreachable_telegram() -> None:
    # Известная беда важнее неизвестности: код должен быть «нездорово», а не
    # «не удалось определить».
    _text, code = report(
        health=TelegramInboxHealth(
            pending_count=9, failed_count=1, head_age_seconds=None
        ),
        webhook=None,
        webhook_error="TelegramNetworkError",
    )

    assert code == EXIT_UNHEALTHY


def test_report_never_prints_the_bot_token() -> None:
    text, _code = report(webhook=None, webhook_error="TelegramNetworkError")

    assert BOT_TOKEN not in text
    assert "AA-secret-bot-token" not in text


def test_a_token_in_the_webhook_url_is_not_printed() -> None:
    # Наш вебхук держит секрет в заголовке, а не в пути, но «токен как
    # секретный путь» — самая частая чужая конфигурация, и увидеть её оператор
    # должен именно тут; напечатать её команда не имеет права.
    text, _code = report(
        webhook=WebhookView(
            url=f"https://yousaid.example/{BOT_TOKEN}",
            pending_update_count=0,
            max_connections=1,
            last_error_at=None,
            last_error_message=None,
        )
    )

    assert BOT_TOKEN not in text
    assert "AA-secret-bot-token" not in text
    assert REDACTED in text


def test_a_webhook_secret_in_the_url_is_not_printed() -> None:
    text, _code = report(
        webhook=WebhookView(
            url=f"https://yousaid.example/telegram/{WEBHOOK_SECRET}",
            pending_update_count=0,
            max_connections=1,
            last_error_at=None,
            last_error_message=None,
        )
    )

    assert WEBHOOK_SECRET not in text
    assert REDACTED in text


def test_a_token_inside_the_telegram_error_text_is_not_printed() -> None:
    # last_error_message — свободный текст от Telegram: что в нём окажется, не
    # гарантирует никто.
    text, _code = report(
        webhook=WebhookView(
            url="https://yousaid.example/telegram/webhook",
            pending_update_count=0,
            max_connections=1,
            last_error_at=NOW - timedelta(minutes=2),
            last_error_message=(
                f"Wrong response: https://api.telegram.org/bot{BOT_TOKEN}/x"
            ),
        )
    )

    assert BOT_TOKEN not in text
    assert "AA-secret-bot-token" not in text
    assert REDACTED in text


def test_a_token_shaped_string_is_redacted_even_if_it_is_not_ours() -> None:
    # Защитный слой не полагается на то, что известные нам значения — все.
    #
    # Хвост НАРОЧНО короче настоящего (у токена Telegram после двоеточия ровно
    # 35 символов): строка обязана попасть под наш шаблон, но не выглядеть
    # рабочим токеном для сканеров секретов — иначе каждый такой тест поднимает
    # ложную тревогу в репозитории. Не «чинить» на более правдоподобную.
    stranger = "0000000000:not-a-real-token-shape"
    text, _code = report(
        webhook=WebhookView(
            url=f"https://yousaid.example/{stranger}",
            pending_update_count=0,
            max_connections=1,
            last_error_at=None,
            last_error_message=None,
        )
    )

    assert stranger not in text
    assert REDACTED in text


def test_a_normal_webhook_url_is_printed_unchanged() -> None:
    text, _code = report()

    assert "https://yousaid.example/telegram/webhook" in text


@pytest.mark.asyncio
async def test_webhook_view_maps_the_fields_the_operator_asks_about() -> None:
    info = WebhookInfo(
        url="https://yousaid.example/telegram/webhook",
        has_custom_certificate=False,
        pending_update_count=7,
        max_connections=1,
        last_error_date=NOW - timedelta(minutes=1),
        last_error_message="Connection timed out",
    )

    view, error = await read_webhook_view(FakeWebhookSource(info))

    assert error is None
    assert view is not None
    assert view.url == "https://yousaid.example/telegram/webhook"
    assert view.pending_update_count == 7
    assert view.max_connections == 1
    assert view.last_error_message == "Connection timed out"
    assert view.last_error_at == NOW - timedelta(minutes=1)


@pytest.mark.asyncio
async def test_unreachable_telegram_reports_only_the_exception_class() -> None:
    view, error = await read_webhook_view(UnreachableWebhookSource())

    assert view is None
    assert error == "RuntimeError"
    assert BOT_TOKEN not in str(error)


@pytest.mark.asyncio
async def test_unreadable_database_reports_an_undetermined_state() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://second_brain_app@127.0.0.1:1/absent",
        schema_database_url="postgresql+asyncpg://second_brain@127.0.0.1:1/absent",
        telegram_bot_token=BOT_TOKEN,
        invite_token_pepper=b"pepper",
        invite_token_pepper_key_id="key-1",
    )

    text, code = await run(settings, NOW)

    assert code == EXIT_UNKNOWN
    assert "database" in text
    assert BOT_TOKEN not in text


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@example")
    monkeypatch.setenv("SCHEMA_DATABASE_URL", "postgresql+asyncpg://owner@example")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "pepper")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "key-1")


def test_alert_thresholds_have_defaults_and_are_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("INBOX_HEAD_AGE_ALERT_SECONDS", raising=False)
    monkeypatch.delenv("INBOX_WEBHOOK_ERROR_WINDOW_SECONDS", raising=False)

    defaults = Settings.from_environment()
    assert defaults.inbox_head_age_alert_seconds == 300
    assert defaults.inbox_webhook_error_window_seconds == 3600

    monkeypatch.setenv("INBOX_HEAD_AGE_ALERT_SECONDS", "60")
    monkeypatch.setenv("INBOX_WEBHOOK_ERROR_WINDOW_SECONDS", "900")

    tuned = Settings.from_environment()
    assert tuned.inbox_head_age_alert_seconds == 60
    assert tuned.inbox_webhook_error_window_seconds == 900


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5"])
def test_head_age_threshold_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("INBOX_HEAD_AGE_ALERT_SECONDS", raw)

    with pytest.raises(RuntimeError, match="INBOX_HEAD_AGE_ALERT_SECONDS"):
        Settings.from_environment()


def test_broken_configuration_reports_an_undetermined_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Настройки читаются ДО защитного try внутри run(): без верхнего
    # обработчика Python сам печатает traceback (а в тексте исключения бывает
    # и DSN, и URL Bot API) и выходит с кодом «нездорово» вместо «не удалось
    # определить».
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")

    with pytest.raises(SystemExit) as exit_info:
        main()

    printed = capsys.readouterr().out
    assert exit_info.value.code == EXIT_UNKNOWN
    assert "RuntimeError" in printed
    assert "Traceback" not in printed
    assert "DATABASE_URL" not in printed
    assert BOT_TOKEN not in printed


def test_a_finished_report_keeps_its_own_verdict_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Верхний обработчик не должен подменять нормальный исход: «Telegram
    # недоступен» — это напечатанные цифры очереди и код 2, а не авария.
    # Исход подставляется вместо запуска цикла: голый asyncio.run() в
    # sync-тесте оставляет pytest-asyncio незакрытый loop (см. test_health).
    _set_required_environment(monkeypatch)
    finished = ("UNREACHABLE: TelegramNetworkError — pending 9", EXIT_UNKNOWN)

    def fake_asyncio_run(coroutine: Coroutine[Any, Any, Any]) -> tuple[str, int]:
        coroutine.close()
        return finished

    monkeypatch.setattr(inbox_status.asyncio, "run", fake_asyncio_run)

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_UNKNOWN
    assert "UNREACHABLE" in capsys.readouterr().out

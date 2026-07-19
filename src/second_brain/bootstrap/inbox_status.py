"""Консольный статус webhook-очереди: что ждёт, что сдохло, что говорит Telegram.

Инструмент runbook'а (эпик API-1, B4). Единственный способ узнать, что дверь
вебхука встала, был «бот перестал отвечать»; эта команда даёт посмотреть
состояние глазами: сколько апдейтов ждёт обработки, сколько отвалилось
насовсем, давно ли лежит голова очереди и что об этом думает сам Telegram.
Она же — опора правила отката «pending растёт → возвращаемся на polling».

Запускается на сервере по SSH. HTTP-эндпоинта тут нет НАМЕРЕННО: он смотрел бы
в интернет и потянул бы за собой вопрос авторизации (отдельный слайс). /health
тоже не трогаем — его дёргает healthcheck контейнера, и глубокая очередь не
должна красить контейнер в unhealthy.

Главное поле telegram-половины — last_error_message: если Telegram не может нам
доставить, он скажет именно там. Свежесть жалобы считается по окну, потому что
last_error Telegram НЕ сбрасывает после удачной доставки (это делает только
setWebhook) — без окна одна давняя ошибка держала бы команду вечно красной.

Код возврата (чтобы команду можно было воткнуть во внешний планировщик без
переделки): 0 — порядок, 1 — нездорово (голова старше порога, есть failed или
Telegram жалуется на доставку), 2 — состояние определить не удалось (очередь не
читается из БД, Telegram недоступен при живой БД, либо команда вообще не дошла
до опроса — например, окружение сконфигурировано мусором). Известная беда
важнее неизвестности: при обеих сразу код 1.

Токен бота не попадает ни в вывод, ни в логи, никаким путём. Из любого
исключения печатается ТОЛЬКО имя класса: текст aiogram/aiohttp может содержать
URL запроса (а URL Bot API включает токен), текст SQLAlchemy — DSN с паролем. А
строки, пришедшие от самого Telegram (url вебхука и текст его жалобы), проходят
через redact(): что в них окажется, не гарантирует никто.
"""

import asyncio
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from aiogram import Bot
from aiogram.types import WebhookInfo

from second_brain.bootstrap.settings import Settings
from second_brain.shared.clock import SystemClock
from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.inbox import (
    PostgresTelegramInboxQueue,
    TelegramInboxHealth,
)

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_UNKNOWN = 2

NOTHING = "-"
REDACTED = "<redacted>"

# Формат токена Telegram: «<цифры>:<буквы-цифры-_-дефис>». Ловит и чужой токен,
# значения которого мы не знаем; длина хвоста взята с запасом вниз, чтобы не
# резать обычные адреса вида host:port/path.
_TOKEN_SHAPE = re.compile(r"\d+:[A-Za-z0-9_-]{16,}")


@dataclass(frozen=True)
class WebhookView:
    """Ответ getWebhookInfo в том объёме, что нужен оператору."""

    url: str | None
    pending_update_count: int
    max_connections: int | None
    last_error_at: datetime | None
    last_error_message: str | None


def redact(value: str, secrets: Sequence[str | None]) -> str:
    """Вычистить секреты из строки, пришедшей от Telegram, перед печатью.

    Печатаются две такие строки — url вебхука и текст жалобы Telegram, — и обе
    приходят извне. Наш вебхук держит секрет в заголовке, а не в пути (иначе он
    утёк бы в access-логи), но «токен как секретный путь вебхука» — настолько
    частая чужая конфигурация, что команда обязана её поймать и НЕ напечатать.
    Точная замена известных значений плюс шаблон токена на случай, когда
    значение нам неизвестно. Это защитный слой, а не разбор строки.
    """
    for secret in secrets:
        if secret:
            value = value.replace(secret, REDACTED)
    return _TOKEN_SHAPE.sub(REDACTED, value)


class WebhookInfoSource(Protocol):
    """Минимум от Bot, нужный команде: один вопрос Telegram про вебхук."""

    async def get_webhook_info(self) -> WebhookInfo: ...


async def read_webhook_view(
    source: WebhookInfoSource,
) -> tuple[WebhookView | None, str | None]:
    """Спросить Telegram про вебхук, не роняя команду при мёртвой сети.

    Возвращает (снимок, None) или (None, имя класса исключения). Именно имя
    класса, а не текст: текст ошибки aiogram/aiohttp может содержать URL
    запроса вместе с токеном бота.
    """
    try:
        info = await source.get_webhook_info()
    except Exception as error:
        return None, type(error).__name__
    return (
        WebhookView(
            url=info.url or None,
            pending_update_count=info.pending_update_count,
            max_connections=info.max_connections,
            last_error_at=info.last_error_date,
            last_error_message=info.last_error_message,
        ),
        None,
    )


def render_report(
    now: datetime,
    *,
    bot_id: int,
    health: TelegramInboxHealth,
    webhook: WebhookView | None,
    webhook_error: str | None,
    head_age_alert_seconds: int,
    webhook_error_window_seconds: int,
    secrets: Sequence[str | None],
) -> tuple[str, int]:
    """Собрать человекочитаемый отчёт и код возврата по двум половинам статуса.

    Половины независимы: недоступный Telegram (webhook=None) не мешает показать
    цифры очереди — оператор всё равно узнает, растёт ли pending.

    secrets — значения, которых в выводе быть не должно (токен бота, секрет
    вебхука): всё, что пришло от Telegram, печатается только через redact().
    """
    problems: list[str] = []
    if health.failed_count:
        problems.append(f"{health.failed_count} update(s) gave up permanently (failed)")
    head_age = health.head_age_seconds
    if head_age is not None and head_age > head_age_alert_seconds:
        problems.append(
            f"the head of the queue has been waiting {head_age:.0f}s "
            f"(threshold {head_age_alert_seconds}s)"
        )

    lines = [
        f"Telegram inbox queue (bot {bot_id})",
        _row("waiting to be processed (pending)", health.pending_count),
        _row("gave up permanently (failed)", health.failed_count),
        _row("head of the queue waiting", _seconds(head_age)),
        _row("stuck above", f"{head_age_alert_seconds}s"),
        "",
        "Telegram side (getWebhookInfo)",
    ]

    if webhook is None:
        lines += [f"  UNREACHABLE: {webhook_error} — the queue numbers still hold", ""]
        if problems:
            return _finish(lines, problems)
        return (
            "\n".join(
                lines
                + [
                    "UNKNOWN: the queue looks fine, but Telegram could not be "
                    "asked about delivery."
                ]
            ),
            EXIT_UNKNOWN,
        )

    error_at = webhook.last_error_at
    error_age = None if error_at is None else (now - error_at).total_seconds()
    error_is_fresh = error_age is not None and error_age <= webhook_error_window_seconds
    if error_is_fresh:
        problems.append("Telegram cannot deliver updates to us (see its last error)")

    lines += [
        _row("url", redact(webhook.url, secrets) if webhook.url else NOTHING),
        _row("waiting at Telegram", webhook.pending_update_count),
        _row(
            "max connections",
            NOTHING if webhook.max_connections is None else webhook.max_connections,
        ),
    ]
    if error_at is None or error_age is None:
        lines.append(_row("last delivery error", "none"))
    else:
        freshness = "FRESH" if error_is_fresh else "history"
        moment = error_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
        lines.append(
            _row("last delivery error", f"{moment} ({error_age:.0f}s ago, {freshness})")
        )
        message = webhook.last_error_message
        lines.append(f"    {redact(message, secrets) if message else NOTHING}")
    lines.append("")

    if problems:
        return _finish(lines, problems)
    return (
        "\n".join(
            lines
            + ["OK: nothing is stuck and Telegram reports no recent delivery error."]
        ),
        EXIT_OK,
    )


def _row(label: str, value: object) -> str:
    """Строка «подпись — значение» одной колонкой, чтобы вывод читался глазами."""
    return f"  {label + ':':<34} {value}"


def _seconds(value: float | None) -> str:
    return NOTHING if value is None else f"{value:.0f}s"


def _finish(lines: list[str], problems: list[str]) -> tuple[str, int]:
    """Итог одной строкой: что именно не так — по порядку находок."""
    return "\n".join(lines + [f"PROBLEM: {'; '.join(problems)}."]), EXIT_UNHEALTHY


async def _read_inbox_health(
    settings: Settings, now: datetime, *, bot_id: int
) -> TelegramInboxHealth:
    """Один агрегат по живым строкам INBOX своим короткоживущим движком."""
    engine = create_database_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            return await PostgresTelegramInboxQueue(session).read_status(
                now, bot_id=bot_id
            )
    finally:
        await engine.dispose()


async def _read_webhook(settings: Settings) -> tuple[WebhookView | None, str | None]:
    """Спросить Telegram про вебхук, закрыв сессию бота при любом исходе."""
    bot = Bot(settings.telegram_bot_token)
    try:
        return await read_webhook_view(bot)
    finally:
        await bot.session.close()


async def run(settings: Settings, now: datetime) -> tuple[str, int]:
    """Собрать отчёт: сначала БД (без неё показывать нечего), потом Telegram."""
    bot_id = settings.telegram_bot_id()
    try:
        health = await _read_inbox_health(settings, now, bot_id=bot_id)
    except Exception as error:
        # Имя класса, а не текст: в сообщении SQLAlchemy может оказаться DSN.
        return (
            "UNKNOWN: the queue state could not be read from the database "
            f"({type(error).__name__}).",
            EXIT_UNKNOWN,
        )
    webhook, webhook_error = await _read_webhook(settings)
    return render_report(
        now,
        bot_id=bot_id,
        health=health,
        webhook=webhook,
        webhook_error=webhook_error,
        head_age_alert_seconds=settings.inbox_head_age_alert_seconds,
        webhook_error_window_seconds=settings.inbox_webhook_error_window_seconds,
        secrets=(settings.telegram_bot_token, settings.telegram_webhook_secret),
    )


def main() -> None:
    """Точка входа команды: любой исход — печатная строка и код возврата.

    Верхний перехват нужен потому, что часть работы идёт ДО защитного try
    внутри run(): чтение настроек, разбор токена, поднятие сессии бота. Без него
    Python напечатал бы traceback (в тексте исключения бывает и DSN с паролем, и
    URL Bot API с токеном) и вышел с кодом 1 — «очередь нездорова», хотя на деле
    состояние определить не удалось. KeyboardInterrupt и SystemExit сюда не
    попадают: они не наследуют Exception.
    """
    try:
        settings = Settings.from_environment()
        report, code = asyncio.run(run(settings, SystemClock().now()))
    except Exception as error:
        print(
            "UNKNOWN: the queue state could not be determined "
            f"({type(error).__name__})."
        )
        sys.exit(EXIT_UNKNOWN)
    print(report)
    sys.exit(code)


if __name__ == "__main__":
    main()

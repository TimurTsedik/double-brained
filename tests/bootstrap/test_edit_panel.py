"""Транспорт правки (S3): промпт режима, отмена, подтверждение, поллер.

Промпт «✏️ Отправьте новый текст» несёт кнопку отмены (edit:cancel);
подтверждение правки задачи с живым напоминанием добавляет строку
«⏰ напоминание осталось на …» (тот же формат, что у reminder-ack). Все три
исхода — fresh-only: replay молчит, generic-ack исключён.
"""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot

from second_brain.shared.i18n import Locale
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)
from tests.identity.locale_fakes import FakeLocaleResolver

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
REMINDER_WHEN = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)


def gateway_with_bot(
    locale: Locale = Locale.RU,
) -> tuple[RecordingAiogramBot, AiogramGateway]:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver(locale)
    )
    return bot, gateway


def callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        value,
        telegram_message_id=update_id + 1_000,
    )


@pytest.mark.asyncio
async def test_edit_prompt_carries_the_cancel_button() -> None:
    bot, gateway = gateway_with_bot()

    await gateway.send_edit_prompt(callback(800, f"edit:note:{UUID(int=1)}"))

    message = bot.sent_messages[0]
    assert message["text"].startswith("✏️ Отправьте новый текст.")
    rows = message["reply_markup"].inline_keyboard
    assert [(button.text, button.callback_data) for row in rows for button in row] == [
        ("✖️ Отмена", "edit:cancel")
    ]


@pytest.mark.asyncio
async def test_edit_prompt_is_localized_in_english() -> None:
    bot, gateway = gateway_with_bot(Locale.EN)

    await gateway.send_edit_prompt(callback(801, f"edit:note:{UUID(int=1)}"))

    assert bot.sent_messages[0]["text"].startswith("✏️ Send the new text.")


@pytest.mark.asyncio
async def test_edit_cancelled_confirmation() -> None:
    bot, gateway = gateway_with_bot()

    await gateway.send_edit_cancelled(callback(802, "edit:cancel"))

    assert bot.sent_messages[0]["text"] == "✖️ Правка отменена."


@pytest.mark.asyncio
async def test_record_edited_confirmation_without_a_reminder() -> None:
    bot, gateway = gateway_with_bot()

    await gateway.send_record_edited(text_update(803, "новый текст"), None)

    assert bot.sent_messages[0]["text"] == "✏️ Запись обновлена."


@pytest.mark.asyncio
async def test_record_edited_confirmation_announces_the_kept_reminder() -> None:
    # Момент уже в tz пространства; формат — как у reminder-ack (⏰ Напомню …).
    bot, gateway = gateway_with_bot()

    await gateway.send_record_edited(text_update(804, "новый текст"), REMINDER_WHEN)

    assert bot.sent_messages[0]["text"] == (
        "✏️ Запись обновлена.\n⏰ напоминание осталось на 19.07.2026 10:00"
    )


@pytest.mark.asyncio
async def test_record_edited_confirmation_is_localized_in_english() -> None:
    bot, gateway = gateway_with_bot(Locale.EN)

    await gateway.send_record_edited(text_update(805, "new text"), REMINDER_WHEN)

    assert bot.sent_messages[0]["text"] == (
        "✏️ Record updated.\n⏰ the reminder stays set for 19.07.2026 10:00"
    )


# ---------------------------------------------------------------------------
# поллер: fresh-only side-effect, generic-ack исключён
# ---------------------------------------------------------------------------


class _EditSpyGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.calls: list[str] = []
        self.edited_payloads: list[datetime | None] = []

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        if offset is not None:
            return []
        return [self._update]

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.calls.append("answer_callback")

    async def send_edit_prompt(self, update: TelegramUpdate) -> None:
        self.calls.append("send_edit_prompt")

    async def send_edit_cancelled(self, update: TelegramUpdate) -> None:
        self.calls.append("send_edit_cancelled")

    async def send_record_edited(
        self, update: TelegramUpdate, reminder_when: datetime | None
    ) -> None:
        self.calls.append("send_record_edited")
        self.edited_payloads.append(reminder_when)

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append("send_acknowledgement")


class _StaticProcessor:
    def __init__(self, result: UpdateResult) -> None:
        self._result = result

    async def process(self, _update: TelegramUpdate) -> UpdateResult:
        return self._result


class _AlwaysLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


def _result(
    kind: AcknowledgementKind,
    *,
    fresh: bool = True,
    reminder_when: datetime | None = None,
) -> UpdateResult:
    return UpdateResult(
        kind, "1" * 32, "2" * 16, fresh=fresh, reminder_when=reminder_when
    )


@pytest.mark.asyncio
async def test_poller_sends_the_edit_prompt_without_generic_ack() -> None:
    update = callback(810, f"edit:note:{UUID(int=1)}")
    gateway = _EditSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(_result(AcknowledgementKind.EDIT_MODE_SET)),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback", "send_edit_prompt"]


@pytest.mark.asyncio
async def test_poller_sends_the_edited_confirmation_with_the_reminder_payload() -> None:
    update = text_update(811, "новый текст")
    gateway = _EditSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(
            _result(AcknowledgementKind.RECORD_EDITED, reminder_when=REMINDER_WHEN)
        ),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["send_record_edited"]
    assert gateway.edited_payloads == [REMINDER_WHEN]


@pytest.mark.asyncio
async def test_poller_stays_silent_on_an_edit_replay() -> None:
    update = text_update(812, "новый текст")
    gateway = _EditSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(_result(AcknowledgementKind.RECORD_EDITED, fresh=False)),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_poller_confirms_the_edit_cancellation() -> None:
    update = callback(813, "edit:cancel")
    gateway = _EditSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(_result(AcknowledgementKind.EDIT_MODE_CANCELLED)),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback", "send_edit_cancelled"]

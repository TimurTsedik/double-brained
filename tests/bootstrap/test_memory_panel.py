from typing import Any, cast

import pytest
from aiogram import Bot

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)
from tests.identity.locale_fakes import FakeLocaleResolver


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


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)


@pytest.mark.asyncio
async def test_gateway_memory_prompt_carries_cancel_button() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_memory_prompt(callback(1, "memory:ask"), question_required=False)

    message = bot.sent_messages[0]
    assert "вопрос" in message["text"].lower()
    assert "parse_mode" not in message
    markup = message["reply_markup"]
    assert [button.callback_data for button in markup.inline_keyboard[0]] == [
        "memory:cancel"
    ]


@pytest.mark.asyncio
async def test_gateway_memory_prompt_reprompts_for_blank_question() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_memory_prompt(callback(2, "memory:ask"), question_required=True)

    assert bot.sent_messages[0]["text"] == "Напишите вопрос."
    assert (
        bot.sent_messages[0]["reply_markup"].inline_keyboard[0][0].callback_data
        == "memory:cancel"
    )


@pytest.mark.asyncio
async def test_gateway_memory_cancelled_is_plain_text() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_memory_cancelled(callback(3, "memory:cancel"))

    assert bot.sent_messages == [{"chat_id": 42, "text": "✖️ Вопрос к памяти отменён."}]


class MemoryGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self.update = update
        self.prompts: list[bool] = []
        self.cancelled = 0
        self.acknowledged: list[AcknowledgementKind] = []

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        return [self.update]

    async def answer_callback(self, _update: TelegramUpdate) -> None:
        return None

    async def send_memory_prompt(
        self, _update: TelegramUpdate, question_required: bool
    ) -> None:
        self.prompts.append(question_required)

    async def send_memory_cancelled(self, _update: TelegramUpdate) -> None:
        self.cancelled += 1

    async def send_acknowledgement(
        self, _update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.acknowledged.append(kind)


class FixedProcessor:
    def __init__(self, kind: AcknowledgementKind) -> None:
        self._kind = kind

    async def process(self, _update: TelegramUpdate) -> UpdateResult:
        return UpdateResult(
            kind=self._kind,
            trace_id="1" * 32,
            span_id="2" * 16,
            fresh=True,
        )


class AlwaysAcquiredLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_poller_routes_memory_mode_set_to_prompt() -> None:
    gateway = MemoryGateway(callback(10, "memory:ask"))
    poller = LocalPoller(
        gateway,
        FixedProcessor(AcknowledgementKind.MEMORY_MODE_SET),
        AlwaysAcquiredLock(),
        sleep=_noop_sleep,
    )

    await poller.run_once()

    assert gateway.prompts == [False]
    assert gateway.acknowledged == []


@pytest.mark.asyncio
async def test_poller_routes_memory_question_queued_to_short_ack() -> None:
    gateway = MemoryGateway(callback(11, "memory:ask"))
    poller = LocalPoller(
        gateway,
        FixedProcessor(AcknowledgementKind.MEMORY_QUESTION_QUEUED),
        AlwaysAcquiredLock(),
        sleep=_noop_sleep,
    )

    await poller.run_once()

    assert gateway.prompts == []
    assert gateway.acknowledged == [AcknowledgementKind.MEMORY_QUESTION_QUEUED]

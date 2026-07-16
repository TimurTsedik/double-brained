"""Авто-панель после действия пользователя (debounce в поллере).

Панель с кнопками досылается через N секунд после ПОСЛЕДНЕГО действия
пользователя в приватном чате, чтобы она всегда была внизу переписки.
0 секунд = фича выключена. Отложенная досылка — best-effort side effect
поллера (как ack), без БД-состояния и без нового result_kind.
"""

import asyncio

import pytest

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.local_updates import AcknowledgementKind

# Крошечная задержка для тестов: реальные секунды в сьюте недопустимы.
TINY_DELAY = 0.05
# Ожидание «заведомо дольше задержки», чтобы поймать досланную панель.
SETTLE = 0.15


class FollowupGateway:
    """Фейковый гейтвей: отдаёт заготовленные апдейты одной пачкой."""

    bot_id = 1

    def __init__(self, updates: list[TelegramUpdate]) -> None:
        self._updates = updates
        self.panels: list[TelegramUpdate] = []
        self.acknowledgements: list[AcknowledgementKind] = []
        self.answered_callbacks: list[TelegramUpdate] = []

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        updates = self._updates
        self._updates = []
        return updates

    async def send_panel(self, update: TelegramUpdate) -> None:
        self.panels.append(update)

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.answered_callbacks.append(update)

    async def send_acknowledgement(
        self, _update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.acknowledgements.append(kind)


class AcquiredPollerLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


class StaticProcessor:
    def __init__(self, result: object) -> None:
        self._result = result

    async def process(self, _update: TelegramUpdate) -> object:
        return self._result


def update_result(kind: AcknowledgementKind, fresh: bool = True) -> object:
    return type("Result", (), {"kind": kind, "fresh": fresh})()


def text_update(update_id: int, text: str = "заметка") -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=text,
    )


@pytest.mark.asyncio
async def test_zero_delay_disables_followup_entirely() -> None:
    gateway = FollowupGateway([text_update(201)])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.CAPTURED)),
        AcquiredPollerLock(),
        panel_followup_seconds=0,
    )

    await poller.run_once()
    await asyncio.sleep(SETTLE)

    assert gateway.panels == []
    await poller.shutdown()


@pytest.mark.asyncio
async def test_processed_action_resends_panel_after_delay_not_immediately() -> None:
    update = text_update(202)
    gateway = FollowupGateway([update])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.CAPTURED)),
        AcquiredPollerLock(),
        panel_followup_seconds=TINY_DELAY,
    )

    await poller.run_once()
    assert gateway.panels == []  # не сразу — только после задержки

    await asyncio.sleep(SETTLE)
    assert gateway.panels == [update]  # досылка = существующий send_panel

    await poller.shutdown()


@pytest.mark.asyncio
async def test_rapid_actions_debounce_to_one_panel_after_the_last() -> None:
    first = text_update(203)
    second = text_update(204)
    gateway = FollowupGateway([first, second])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.CAPTURED)),
        AcquiredPollerLock(),
        panel_followup_seconds=TINY_DELAY,
    )

    await poller.run_once()
    await asyncio.sleep(SETTLE)

    assert gateway.panels == [second]

    await poller.shutdown()


@pytest.mark.asyncio
async def test_panel_shown_result_does_not_schedule_second_panel() -> None:
    update = text_update(205, "/start")
    gateway = FollowupGateway([update])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.PANEL_SHOWN)),
        AcquiredPollerLock(),
        panel_followup_seconds=TINY_DELAY,
    )

    await poller.run_once()
    assert gateway.panels == [update]  # ответ и был панелью

    await asyncio.sleep(SETTLE)
    assert gateway.panels == [update]  # второй панели подряд нет

    await poller.shutdown()


@pytest.mark.asyncio
async def test_ignored_stranger_update_does_not_schedule_followup() -> None:
    gateway = FollowupGateway([text_update(206)])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.IGNORED)),
        AcquiredPollerLock(),
        panel_followup_seconds=TINY_DELAY,
    )

    await poller.run_once()
    await asyncio.sleep(SETTLE)

    assert gateway.panels == []
    await poller.shutdown()


@pytest.mark.asyncio
async def test_non_private_update_does_not_schedule_followup() -> None:
    group_update = TelegramUpdate(
        bot_id=1,
        update_id=207,
        is_private=False,
        telegram_user_id=42,
        text="в группе",
    )
    gateway = FollowupGateway([group_update])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.CAPTURED)),
        AcquiredPollerLock(),
        panel_followup_seconds=TINY_DELAY,
    )

    await poller.run_once()
    await asyncio.sleep(SETTLE)

    assert gateway.panels == []
    await poller.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_followup_cleanly() -> None:
    gateway = FollowupGateway([text_update(208)])
    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.CAPTURED)),
        AcquiredPollerLock(),
        panel_followup_seconds=TINY_DELAY,
    )

    await poller.run_once()
    await poller.shutdown()  # отменяет и ДОЖИДАЕТСЯ висящую досылку

    await asyncio.sleep(SETTLE)
    assert gateway.panels == []  # панель после shutdown не приходит

    # Никаких висящих задач после shutdown (иначе -W error уронит сьюту
    # «Task was destroyed but it is pending!» при закрытии цикла).
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []

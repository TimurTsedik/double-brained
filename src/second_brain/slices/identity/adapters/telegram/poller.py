"""Long-polling цикл Telegram: получить апдейт → обработать → показать ответ.

Сама презентация «result.kind → вызовы гейтвея» живёт в
``TelegramPresenter`` (presenter.py) и переиспользуется webhook-путём;
поллер отвечает только за цикл getUpdates, ретрай обработчика, offset
и досылку панели (debounce).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.presenter import (
    TelegramGateway,
    TelegramPresenter,
)
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)

__all__ = [
    "LocalPoller",
    "PollerAlreadyRunning",
    "TelegramGateway",
    "WebhookConfigured",
]


class WebhookConfigured(RuntimeError):
    pass


class PollerAlreadyRunning(RuntimeError):
    pass


class UpdateProcessor(Protocol):
    async def process(self, update: TelegramUpdate) -> UpdateResult: ...


class PollerLock(Protocol):
    async def acquire(self, bot_id: int) -> bool: ...


# После этих результатов панель досылать не надо: IGNORED — апдейт не от
# зачисленного пользователя в привате; PANEL_SHOWN/LANGUAGE_SELECTED — ответом
# уже была сама панель (иначе пользователь получит две панели подряд).
_PANEL_FOLLOWUP_SKIP_KINDS = frozenset(
    {
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.PANEL_SHOWN,
        AcknowledgementKind.LANGUAGE_SELECTED,
    }
)


class LocalPoller:
    def __init__(
        self,
        gateway: TelegramGateway,
        processor: UpdateProcessor,
        lock: PollerLock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        panel_followup_seconds: float = 0,
    ) -> None:
        self._gateway = gateway
        self._processor = processor
        self._presenter = TelegramPresenter(gateway, sleep=sleep)
        self._lock = lock
        self._sleep = sleep
        self._started = False
        self.offset: int | None = None
        # Досылка панели через N секунд после ПОСЛЕДНЕГО действия в чате
        # (0 = выключено). Состояние только в памяти: упавший процесс теряет
        # висящую досылку — принято, следующее действие перепланирует.
        self._panel_followup_seconds = panel_followup_seconds
        self._panel_followups: dict[int, asyncio.Task[None]] = {}

    async def run_once(self) -> None:
        if not self._started:
            if await self._gateway.configured_webhook_url():
                raise WebhookConfigured("local polling refuses a configured webhook")
            bot_id = getattr(self._gateway, "bot_id", None)
            if bot_id is not None and not await self._lock.acquire(bot_id):
                raise PollerAlreadyRunning("another local poller holds this bot lock")
            updates = await self._gateway.get_updates(
                None, ["message", "callback_query"]
            )
            if (
                bot_id is None
                and updates
                and not await self._lock.acquire(updates[0].bot_id)
            ):
                raise PollerAlreadyRunning("another local poller holds this bot lock")
            self._started = True
        else:
            updates = await self._gateway.get_updates(
                self.offset, ["message", "callback_query"]
            )

        for update in updates:
            if update.callback_query_id is not None:
                try:
                    await self._gateway.answer_callback(update)
                except Exception:
                    pass
            while True:
                try:
                    result = await self._processor.process(update)
                except Exception:
                    await self._sleep(1.0)
                    continue
                break
            await self._presenter.present(update, result)
            self.offset = update.update_id + 1
            await self._reschedule_panel_followup(update, result)

    async def shutdown(self) -> None:
        """Отменяет и дожидается все висящие досылки панели.

        Обязателен перед закрытием event loop: незавершённая asyncio-задача
        при закрытии цикла даёт «Task was destroyed but it is pending!».
        """
        while self._panel_followups:
            _chat, pending = self._panel_followups.popitem()
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass

    async def _reschedule_panel_followup(
        self, update: TelegramUpdate, result: UpdateResult
    ) -> None:
        """Debounce по чату: новое действие отменяет прежнюю досылку панели."""
        if self._panel_followup_seconds <= 0:
            return
        if not update.is_private or update.telegram_user_id is None:
            return
        if result.kind in _PANEL_FOLLOWUP_SKIP_KINDS:
            return
        chat_id = update.telegram_user_id
        previous = self._panel_followups.pop(chat_id, None)
        if previous is not None:
            previous.cancel()
            try:
                await previous
            except asyncio.CancelledError:
                pass
        self._panel_followups[chat_id] = asyncio.create_task(
            self._send_panel_followup(chat_id, update)
        )

    async def _send_panel_followup(self, chat_id: int, update: TelegramUpdate) -> None:
        await asyncio.sleep(self._panel_followup_seconds)
        # Best-effort (как ack): досылка панели — не receipted-результат,
        # и сама она — отправка бота, ничего не перепланирует.
        try:
            await self._gateway.send_panel(update)
        except Exception:
            pass
        self._panel_followups.pop(chat_id, None)

"""Debounce-досылка панели с кнопками после действия пользователя.

Панель досылается через N секунд после ПОСЛЕДНЕГО действия в приватном чате
(0 = фича выключена). Логика вынесена из поллера без изменений и
переиспользуется двумя потребителями с одинаковым поведением: поллером
(long-polling) и inbox-шагом воркера (webhook-путь). Состояние только в
памяти: упавший процесс теряет висящую досылку — принято, следующее действие
перепланирует.
"""

import asyncio
from typing import Protocol

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)

# После этих результатов панель досылать не надо: IGNORED — апдейт не от
# зачисленного пользователя в привате; PANEL_SHOWN/LANGUAGE_SELECTED — ответом
# уже была сама панель (иначе пользователь получит две панели подряд).
PANEL_FOLLOWUP_SKIP_KINDS = frozenset(
    {
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.PANEL_SHOWN,
        AcknowledgementKind.LANGUAGE_SELECTED,
    }
)


class PanelSender(Protocol):
    async def send_panel(self, update: TelegramUpdate) -> None: ...


class PanelFollowupScheduler:
    """Debounce по чату: новое действие отменяет прежнюю досылку панели."""

    def __init__(self, gateway: PanelSender, panel_followup_seconds: float = 0) -> None:
        self._gateway = gateway
        self._panel_followup_seconds = panel_followup_seconds
        self._panel_followups: dict[int, asyncio.Task[None]] = {}

    async def reschedule(self, update: TelegramUpdate, result: UpdateResult) -> None:
        if self._panel_followup_seconds <= 0:
            return
        if not update.is_private or update.telegram_user_id is None:
            return
        if result.kind in PANEL_FOLLOWUP_SKIP_KINDS:
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

    async def _send_panel_followup(self, chat_id: int, update: TelegramUpdate) -> None:
        await asyncio.sleep(self._panel_followup_seconds)
        # Best-effort (как ack): досылка панели — не receipted-результат,
        # и сама она — отправка бота, ничего не перепланирует.
        try:
            await self._gateway.send_panel(update)
        except Exception:
            pass
        self._panel_followups.pop(chat_id, None)

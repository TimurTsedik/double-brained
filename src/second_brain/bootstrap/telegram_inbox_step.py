"""Inbox-шаг воркера: обработка webhook-очереди telegram_update_inbox.

Шаг того же цикла local_voice_worker (нового процесса НЕТ), но ГЛОБАЛЬНЫЙ —
вызывается один раз за тик ДО пер-space шагов: пользователь резолвится
процессором ПОСЛЕ, inbox пер-пространственным не бывает.

Модель — claimed-work по образцу PageTitleFetchStep: claim ОДНОЙ головной
строки своей транзакцией (lease: attempt_count++ и next_attempt_at вперёд),
обработка СТРОГО вне её, итог (done/failed) отдельной транзакцией. Порядок —
строго по update_id внутри бота: незрелая голова блокирует хвост,
failed-голова — нет (см. PostgresTelegramInboxQueue).

Обработка повторяет поллер байт-в-байт: normalize → best-effort
answer_callback ДО обработки → LocalUpdateProcessor.process →
TelegramPresenter.present → debounce-досылка панели. Частичные повторы после
краха гасит receipt-идемпотентность процессора (fresh=False → present
молчит) — как у поллера при ретрае.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.adapters.persistence.inbox import (
    ClaimedInboxUpdate,
    PostgresTelegramInboxQueue,
    TelegramInboxHealth,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.followup import (
    PanelFollowupScheduler,
)
from second_brain.slices.identity.adapters.telegram.gateway import (
    normalize_aiogram_update,
)
from second_brain.slices.identity.adapters.telegram.presenter import (
    TelegramGateway,
    TelegramPresenter,
)
from second_brain.slices.identity.application.local_updates import UpdateResult


class UpdateProcessor(Protocol):
    async def process(self, update: TelegramUpdate) -> UpdateResult: ...


class TelegramInboxStep:
    """Шаг воркера: догнать все созревшие головы INBOX, по одной за транзакцию."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: TelegramGateway,
        processor: UpdateProcessor,
        *,
        max_attempts: int,
        retry_backoff: timedelta,
        panel_followup_seconds: float = 0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._processor = processor
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff
        self._presenter = TelegramPresenter(gateway, sleep=sleep)
        self._panel_followups = PanelFollowupScheduler(gateway, panel_followup_seconds)

    async def process_once(self, now: datetime) -> bool:
        worked = False
        while True:
            claimed = await self._claim(now)
            if claimed is None:
                return worked
            worked = True
            if await self._handle(claimed):
                await self._mark_done(claimed, now)
            else:
                await self._record_failure(claimed)

    async def _handle(self, claimed: ClaimedInboxUpdate) -> bool:
        try:
            update = normalize_aiogram_update(
                Update.model_validate(claimed.payload), claimed.bot_id
            )
        except Exception:
            # Яд (payload не парсится) — мягкий сбой: бэкофф → failed,
            # хвост не блокируется навсегда.
            return False
        if update.callback_query_id is not None:
            # Как поллер: best-effort ack кнопки ДО обработки.
            try:
                await self._gateway.answer_callback(update)
            except Exception:
                pass
        try:
            result = await self._processor.process(update)
            await self._presenter.present(update, result)
            await self._panel_followups.reschedule(update, result)
        except Exception:
            # Сбой транзакции/презентации: строка ждёт свой бэкофф; повтор
            # гасится receipt-идемпотентностью (fresh=False → молчание).
            return False
        return True

    async def _claim(self, now: datetime) -> ClaimedInboxUpdate | None:
        # Бот у шага один — гейтвея: claim и статус ходят только по его строкам.
        async with self._session_factory() as session, session.begin():
            return await PostgresTelegramInboxQueue(session).claim_head(
                now,
                bot_id=self._gateway.bot_id,
                max_attempts=self._max_attempts,
                retry_backoff=self._retry_backoff,
            )

    async def _mark_done(self, claimed: ClaimedInboxUpdate, now: datetime) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresTelegramInboxQueue(session).mark_done(claimed.id, now)

    async def _record_failure(self, claimed: ClaimedInboxUpdate) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresTelegramInboxQueue(session).record_failure(
                claimed.id, max_attempts=self._max_attempts
            )

    async def read_status(self, now: datetime) -> TelegramInboxHealth:
        """Глубина pending/failed и возраст головы — для статуса/монитора (B4)."""
        async with self._session_factory() as session:
            return await PostgresTelegramInboxQueue(session).read_status(
                now, bot_id=self._gateway.bot_id
            )

    async def shutdown(self) -> None:
        """Отменяет висящие досылки панели перед закрытием event loop."""
        await self._panel_followups.shutdown()

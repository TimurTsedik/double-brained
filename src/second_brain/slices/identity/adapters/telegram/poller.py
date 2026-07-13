import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)


class WebhookConfigured(RuntimeError):
    pass


class PollerAlreadyRunning(RuntimeError):
    pass


class TelegramGateway(Protocol):
    bot_id: int

    async def configured_webhook_url(self) -> str | None: ...

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]: ...

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None: ...


class UpdateProcessor(Protocol):
    async def process(self, update: TelegramUpdate) -> UpdateResult: ...


class PollerLock(Protocol):
    async def acquire(self, bot_id: int) -> bool: ...


class LocalPoller:
    def __init__(
        self,
        gateway: TelegramGateway,
        processor: UpdateProcessor,
        lock: PollerLock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gateway = gateway
        self._processor = processor
        self._lock = lock
        self._sleep = sleep
        self._started = False
        self.offset: int | None = None

    async def run_once(self) -> None:
        if not self._started:
            if await self._gateway.configured_webhook_url():
                raise WebhookConfigured("local polling refuses a configured webhook")
            bot_id = getattr(self._gateway, "bot_id", None)
            if bot_id is not None and not await self._lock.acquire(bot_id):
                raise PollerAlreadyRunning("another local poller holds this bot lock")
            updates = await self._gateway.get_updates(None, ["message"])
            if (
                bot_id is None
                and updates
                and not await self._lock.acquire(updates[0].bot_id)
            ):
                raise PollerAlreadyRunning("another local poller holds this bot lock")
            self._started = True
        else:
            updates = await self._gateway.get_updates(self.offset, ["message"])

        for update in updates:
            while True:
                try:
                    result = await self._processor.process(update)
                except Exception:
                    await self._sleep(1.0)
                    continue
                break
            self.offset = update.update_id + 1
            if result.kind not in {
                AcknowledgementKind.IGNORED,
                AcknowledgementKind.CAPTURED,
            }:
                try:
                    await self._gateway.send_acknowledgement(update, result.kind)
                except Exception:
                    pass

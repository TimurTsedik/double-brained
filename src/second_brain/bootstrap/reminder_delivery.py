from datetime import datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.adapters.telegram.messages import (
    reminder_delivered_text,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
    WorkerIdentityPort,
)
from second_brain.slices.reminders.adapters.persistence.repository import (
    PostgresReminderWriter,
)
from second_brain.slices.reminders.application.contracts import (
    ClaimedReminder,
    ReminderDeliveryPort,
)


class AiogramReminderDelivery:
    """ReminderDeliveryPort over aiogram. Plain text, no parse_mode: the reminder
    text is the user's task title and must never be interpreted as markup."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def deliver(self, text: str, recipient: TelegramRecipient) -> None:
        await self._bot.send_message(recipient.telegram_user_id, text)


class _SendFailed(Exception):
    """Internal signal: the Telegram send inside a claimed unit blew up."""

    def __init__(self, claimed: ClaimedReminder, cause: Exception) -> None:
        super().__init__(str(cause))
        self.claimed = claimed


class ReminderDeliveryStep:
    """Delivers due reminders inside the umbrella worker loop.

    Mirrors the memory claimed-work model (M1/M5): each claimed unit is ONE
    reminder in ONE transaction — claim under ``FOR UPDATE SKIP LOCKED``, send,
    mark sent, commit, then take the next one. Overlapping ticks skip each
    other's locked rows, so a reminder is never sent twice; a send failure rolls
    back only its own claim and never un-marks earlier successful deliveries.

    A failed send is then recorded in a compensating transaction: attempts += 1
    with a linear backoff on ``next_attempt_at``; after MAX_SEND_ATTEMPTS the
    reminder turns ``failed`` and is never claimed again. So a permanently
    broken send neither spams Telegram every tick nor starves later reminders
    of the same space (the loop simply claims the next due row).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        delivery_port: ReminderDeliveryPort,
        identity: WorkerIdentityPort,
    ) -> None:
        self._session_factory = session_factory
        self._delivery_port = delivery_port
        self._identity = identity

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        # Догоняем все созревшие — по одному claimed-unit на транзакцию (M5).
        worked = False
        while await self._deliver_one(access_context, now):
            worked = True
        return worked

    async def _deliver_one(self, access_context: AccessContext, now: datetime) -> bool:
        try:
            return await self._claim_and_send(access_context, now)
        except _SendFailed as failure:
            # Claim-транзакция уже откатилась (строка не потрогана, лок снят) —
            # фиксируем попытку/бэкофф отдельной транзакцией и идём дальше.
            await self._record_send_failure(access_context, failure.claimed, now)
            return True

    async def _claim_and_send(
        self, access_context: AccessContext, now: datetime
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            writer = PostgresReminderWriter(session)
            claimed = await writer.claim_due(access_context, now)
            if claimed is None:
                return False
            locale = await self._identity.resolve_locale(access_context)
            recipient = await self._identity.resolve_telegram_recipient(access_context)
            try:
                await self._delivery_port.deliver(
                    reminder_delivered_text(claimed.text, locale), recipient
                )
            except Exception as error:
                raise _SendFailed(claimed, error) from error
            await writer.mark_sent(access_context, claimed.reminder_id, now)
            return True

    async def _record_send_failure(
        self, access_context: AccessContext, claimed: ClaimedReminder, now: datetime
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresReminderWriter(session).record_send_failure(
                access_context, claimed.reminder_id, now
            )

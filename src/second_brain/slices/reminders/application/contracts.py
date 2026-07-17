from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
    UpdateTransaction,
)
from second_brain.slices.reminders.domain.entities import Reminder

# Дефолт пространства (совпадает с user_spaces.timezone по умолчанию); подстраховка
# на случай, если owner-предикатный read вернул None.
DEFAULT_TIMEZONE = "Asia/Jerusalem"


@dataclass(frozen=True)
class CreateReminderCommand:
    access_context: AccessContext
    remind_at: datetime
    text: str = field(repr=False)
    source_task_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CancelReminderForTaskCommand:
    access_context: AccessContext
    source_task_id: UUID
    cancelled_at: datetime


@dataclass(frozen=True)
class ClaimedReminder:
    """One reminder captured under a row lock for the delivery transaction."""

    reminder_id: UUID = field(repr=False)
    text: str = field(repr=False)
    trace_id: str = field(repr=False)


class TimeExtractor(Protocol):
    """Deterministic natural-language due-time parser at the slice boundary.

    Given a task title, the current instant, and the space timezone, returns the
    first FUTURE instant explicitly named in the text, or ``None`` when the text
    carries no explicit time-of-day. Kept behind a port so the engine
    (``dateparser``) stays swappable and the capture flow stays testable.
    """

    def extract_due(self, text: str, now: datetime, tz: str) -> datetime | None: ...

    def might_contain_due(self, text: str) -> bool:
        """Копеечный tz-независимый прескрин: МОЖЕТ ли в тексте быть время-суток.

        Позволяет вызывающему пропустить резолв часового пояса (поход в базу)
        для обычной заметки без времени. ``True`` не гарантирует напоминание —
        только что стоит запускать полный разбор.
        """
        ...


class SpaceTimezoneReader(Protocol):
    async def resolve_timezone(self, access_context: AccessContext) -> str: ...


class ReminderWriter(Protocol):
    async def create_reminder(self, command: CreateReminderCommand) -> Reminder: ...

    async def cancel_for_task(self, command: CancelReminderForTaskCommand) -> None: ...


class ReminderAckReader(Protocol):
    """Reads back the pending reminder just set from a capture, for the ack.

    Returns the ``remind_at`` rendered in the space timezone (or ``None`` when the
    captured task carried no due time), so the confirmation can announce it.
    """

    async def reminder_for_capture(
        self,
        access_context: AccessContext,
        capture_event_id: UUID,
        transaction: UpdateTransaction,
    ) -> datetime | None: ...


class ReminderDeliveryPort(Protocol):
    """Sends one message; returns the Telegram message_id of the sent message.

    Возвращённый message_id — доказательство доставки: доставка напоминаний
    сохраняет его на строке (mark_sent), подтверждения «⏰ Напомню…» его
    игнорируют.
    """

    async def deliver(self, text: str, recipient: TelegramRecipient) -> int: ...

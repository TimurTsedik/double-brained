from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)


@dataclass(frozen=True)
class TelegramContactPayload:
    """Нормализованный ``message.contact`` из Telegram-обновления.

    Номер и имена — PII: живут только в памяти прохода (repr-hidden), в
    receipt/логи не попадают. Маршрутизация карточки идёт по ОТПРАВИТЕЛЮ
    (``message.from_user.id``), никогда по ``contact.user_id`` — поэтому его
    здесь нет вовсе.
    """

    phone_number: str = field(repr=False)
    first_name: str = field(repr=False)
    last_name: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SaveContactCommand:
    access_context: AccessContext
    display_name: str = field(repr=False)
    phone_number: str = field(repr=False)
    saved_at: datetime
    trace_id: str


class ContactIntakePort(Protocol):
    """Upsert контакта внутри receipt-транзакции (идемпотентность = receipt)."""

    async def save(
        self, command: SaveContactCommand, transaction: UpdateTransaction
    ) -> None: ...

"""Опубликованные контракты правки записи (S3, спека §3).

Правка живёт pending-режимом по образцу поиска/памяти: кнопка «✏️ Править»
ставит режим, СЛЕДУЮЩЕЕ сообщение становится новым текстом записи. Последствия
правки (UPDATE текста, пере-индексация БЕЗ пере-классификации, пересбор
sidecar-ссылок, строка «⏰ напоминание осталось…» для задач) собирает
bootstrap-композиция за этим портом.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.capture.application.contracts import TelegramLink
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.retrieval.application.contracts import SearchRecordType


@dataclass(frozen=True)
class BeginRecordEditCommand:
    """Поставить pending-режим правки на конкретную запись.

    Владение и существование записи проверяются ПРИ УСТАНОВКЕ (owner-предикат
    + RLS): чужой/несуществующий id → режим не ставится (False), поведение
    снаружи неотличимо от мусорного callback'а.
    """

    access_context: AccessContext = field(repr=False)
    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ConsumeRecordEditCommand:
    """Потребить pending-режим правки следующим текстовым сообщением."""

    access_context: AccessContext = field(repr=False)
    text: str = field(repr=False)
    # Ссылки нового текста (message.entities): sidecar record_urls записи
    # пересобирается под новый текст (label/url — PII, вне repr).
    links: tuple[TelegramLink, ...] = field(repr=False)
    received_at: datetime
    trace_id: str


@dataclass(frozen=True)
class RecordEditResult:
    """Итог потреблённой правки — transient-payload для подтверждения."""

    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)
    # Повторная проверка при consume не нашла запись (владение/существование):
    # режим потреблён, правка не применена — снаружи IGNORED.
    record_missing: bool = False
    # Прислан пробельный «новый текст»: правка НЕ применена, режим ОСТАЛСЯ
    # ждать настоящий текст — снаружи повтор промпта режима.
    text_required: bool = False
    # Живое (pending) напоминание правленой задачи, момент уже в tz
    # пространства: ack добавляет строку «⏰ напоминание осталось на …».
    # Будильник правкой НЕ трогается (решение владельца §6.2).
    reminder_when: datetime | None = None


class RecordEditPort(Protocol):
    """Правка записи внутри существующей update-транзакции."""

    async def begin(
        self, command: BeginRecordEditCommand, transaction: UpdateTransaction
    ) -> bool: ...

    async def cancel(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> None: ...

    async def consume_new_text(
        self, command: ConsumeRecordEditCommand, transaction: UpdateTransaction
    ) -> RecordEditResult | None: ...

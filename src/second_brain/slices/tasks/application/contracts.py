from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.tasks.domain.entities import (
    PendingCaptureType as PendingCaptureType,
)


@dataclass(frozen=True)
class CreateTaskCommand:
    access_context: AccessContext
    title: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CreateTypedCaptureCommand:
    access_context: AccessContext
    selection: PendingCaptureType
    text: str = field(repr=False)
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str
    # Интерактивный ввод (текст/голос): дефолтная заметка с явным будущим
    # временем сама становится напоминанием-задачей. Классификатор его НЕ ставит
    # (под-пункты сохраняют выбранный ИИ тип).
    route_default_by_time: bool = False
    # Часовой пояс запроса, в котором разбирать «завтра в 9». Задаёт только
    # HTTP-путь; телеграм и воркеры не передают ничего и работают поясом
    # пространства, как раньше.
    request_tz: str | None = None


@dataclass(frozen=True)
class TaskListItem:
    id: UUID
    title: str = field(repr=False)


@dataclass(frozen=True)
class TaskPanelResult:
    items: tuple[TaskListItem, ...]
    completion_changed: bool | None


@dataclass(frozen=True)
class CompleteTaskCommand:
    access_context: AccessContext
    task_id: UUID
    completed_at: datetime
    trace_id: str


@dataclass(frozen=True)
class RenameTaskCommand:
    """Правка (S3): заменить title задачи + бампнуть updated_at.

    Статус, напоминание и происхождение задачи правкой НЕ трогаются
    (решение владельца §6.2: время не пере-извлекаем, будильник стоит).
    """

    access_context: AccessContext
    task_id: UUID = field(repr=False)
    title: str = field(repr=False)
    updated_at: datetime


@dataclass(frozen=True)
class SetAwaitingTaskCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class SetPendingCaptureSelectionCommand:
    access_context: AccessContext
    selection: str
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CancelPendingTaskCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ConsumePendingTaskTextCommand:
    access_context: AccessContext
    text: str | None = field(repr=False)
    is_private_chat: bool
    telegram_message_id: int | None
    source_capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ConsumePendingCaptureSelectionCommand:
    access_context: AccessContext
    consumed_at: datetime
    trace_id: str


class TaskModePort(Protocol):
    """Public task-mode boundary for work inside an existing update transaction."""

    async def set_awaiting_task(
        self, command: SetAwaitingTaskCommand, transaction: UpdateTransaction
    ) -> None: ...

    async def set_selection(
        self, command: SetPendingCaptureSelectionCommand, transaction: UpdateTransaction
    ) -> None: ...

    async def cancel(
        self, command: CancelPendingTaskCommand, transaction: UpdateTransaction
    ) -> None: ...


class TaskPanelPort(Protocol):
    async def list_open(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> TaskPanelResult: ...

    async def complete(
        self, command: CompleteTaskCommand, transaction: UpdateTransaction
    ) -> TaskPanelResult: ...

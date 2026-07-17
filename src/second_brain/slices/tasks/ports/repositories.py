from typing import Protocol

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    CompleteTaskCommand,
    ConsumePendingCaptureSelectionCommand,
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType, Task


class TaskWriter(Protocol):
    async def create(self, command: CreateTaskCommand) -> Task: ...


class TaskPanelStore(Protocol):
    async def list_inbox(
        self, access_context: AccessContext, limit: int
    ) -> tuple[Task, ...]: ...

    async def complete(self, command: CompleteTaskCommand) -> bool: ...


class PendingCaptureSelectionStore(Protocol):
    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None: ...
    async def set_selection(
        self, command: SetPendingCaptureSelectionCommand
    ) -> None: ...

    async def cancel(self, command: CancelPendingTaskCommand) -> None: ...

    async def consume_selection(
        self, command: ConsumePendingCaptureSelectionCommand
    ) -> PendingCaptureType | None: ...

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None: ...

from typing import Protocol

from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType, Task


class TaskWriter(Protocol):
    async def create(self, command: CreateTaskCommand) -> Task: ...


class PendingCaptureSelectionStore(Protocol):
    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None: ...
    async def set_selection(
        self, command: SetPendingCaptureSelectionCommand
    ) -> None: ...

    async def cancel(self, command: CancelPendingTaskCommand) -> None: ...

    async def consume_selection(
        self, command: ConsumePendingTaskTextCommand
    ) -> PendingCaptureType: ...

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None: ...

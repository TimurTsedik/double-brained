from typing import Protocol

from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.domain.entities import Task


class TaskWriter(Protocol):
    async def create(self, command: CreateTaskCommand) -> Task: ...


class PendingTaskModeStore(Protocol):
    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None: ...

    async def cancel(self, command: CancelPendingTaskCommand) -> None: ...

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None: ...

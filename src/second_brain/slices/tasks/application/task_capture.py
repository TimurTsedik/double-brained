from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingTaskTextCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.domain.entities import Task
from second_brain.slices.tasks.ports.repositories import PendingTaskModeStore


class TaskCapture:
    def __init__(self, pending_task_mode_store: PendingTaskModeStore) -> None:
        self._pending_task_mode_store = pending_task_mode_store

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        await self._pending_task_mode_store.set_awaiting_task(command)

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        await self._pending_task_mode_store.cancel(command)

    async def consume_for_text(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        if not _is_eligible(command):
            return None
        return await self._pending_task_mode_store.consume_awaiting_task(command)


def _is_eligible(command: ConsumePendingTaskTextCommand) -> bool:
    return (
        command.is_private_chat
        and command.text is not None
        and command.text != ""
        and command.telegram_message_id is not None
        and not command.text.lstrip().startswith("/")
    )

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.application.contracts import (
    CompleteTaskCommand,
    TaskListItem,
    TaskPanelResult,
)
from second_brain.slices.tasks.ports.repositories import TaskPanelStore

TASK_PANEL_LIMIT = 10


class TaskPanel:
    def __init__(self, store: TaskPanelStore) -> None:
        self._store = store

    async def list_open(self, access_context: AccessContext) -> TaskPanelResult:
        tasks = await self._store.list_inbox(access_context, TASK_PANEL_LIMIT)
        return TaskPanelResult(
            items=tuple(TaskListItem(id=task.id, title=task.title) for task in tasks),
            completion_changed=None,
        )

    async def complete(self, command: CompleteTaskCommand) -> TaskPanelResult:
        changed = await self._store.complete(command)
        refreshed = await self.list_open(command.access_context)
        return TaskPanelResult(
            items=refreshed.items,
            completion_changed=changed,
        )

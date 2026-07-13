from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.application.contracts import CompleteTaskCommand
from second_brain.slices.tasks.application.task_panel import TaskPanel
from second_brain.slices.tasks.domain.entities import Task, TaskStatus
from second_brain.slices.tasks.ports.repositories import TaskPanelStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


def task(index: int, status: TaskStatus = TaskStatus.INBOX) -> Task:
    return Task(
        id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
        user_space_id=ACCESS.user_space_id,
        title=f"task {index}",
        description=None,
        status=status,
        source_capture_event_id=UUID(f"10000000-0000-0000-0000-{index:012d}"),
        created_at=NOW,
        updated_at=NOW,
        trace_id="1" * 32,
    )


class InMemoryTaskPanelStore(TaskPanelStore):
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks
        self.limits: list[int] = []

    async def list_inbox(
        self, access_context: AccessContext, limit: int
    ) -> tuple[Task, ...]:
        self.limits.append(limit)
        return tuple(
            item
            for item in self.tasks
            if item.user_space_id == access_context.user_space_id
            and item.status is TaskStatus.INBOX
        )[:limit]

    async def complete(self, command: CompleteTaskCommand) -> bool:
        for index, item in enumerate(self.tasks):
            if (
                item.id == command.task_id
                and item.user_space_id == command.access_context.user_space_id
                and item.status is TaskStatus.INBOX
            ):
                self.tasks[index] = replace(
                    item,
                    status=TaskStatus.COMPLETED,
                    updated_at=command.completed_at,
                )
                return True
        return False


@pytest.mark.asyncio
async def test_list_open_returns_ten_items_without_titles_in_repr() -> None:
    store = InMemoryTaskPanelStore([task(index) for index in range(1, 12)])

    result = await TaskPanel(store).list_open(ACCESS)

    assert store.limits == [10]
    assert [item.title for item in result.items] == [
        f"task {index}" for index in range(1, 11)
    ]
    assert result.completion_changed is None
    assert "task 1" not in repr(result)


@pytest.mark.asyncio
async def test_complete_changes_one_inbox_task_and_returns_refreshed_list() -> None:
    store = InMemoryTaskPanelStore([task(1), task(2)])

    result = await TaskPanel(store).complete(
        CompleteTaskCommand(
            access_context=ACCESS,
            task_id=task(1).id,
            completed_at=LATER,
            trace_id="2" * 32,
        )
    )

    assert result.completion_changed is True
    assert [item.id for item in result.items] == [task(2).id]
    assert store.tasks[0].status is TaskStatus.COMPLETED
    assert store.tasks[0].updated_at == LATER
    assert store.tasks[0].trace_id == "1" * 32


@pytest.mark.asyncio
async def test_complete_already_completed_task_is_safe_and_unchanged() -> None:
    completed = task(1, TaskStatus.COMPLETED)
    store = InMemoryTaskPanelStore([completed])

    result = await TaskPanel(store).complete(
        CompleteTaskCommand(
            access_context=ACCESS,
            task_id=completed.id,
            completed_at=LATER,
            trace_id="2" * 32,
        )
    )

    assert result.completion_changed is False
    assert result.items == ()
    assert store.tasks == [completed]

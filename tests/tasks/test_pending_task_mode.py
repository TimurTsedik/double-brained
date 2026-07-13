from datetime import UTC, datetime
from uuid import UUID

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingTaskTextCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.application.task_capture import TaskCapture
from second_brain.slices.tasks.domain.entities import (
    PendingCaptureMode,
    Task,
    TaskStatus,
)
from second_brain.slices.tasks.ports.repositories import PendingTaskModeStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)
SOURCE_A = UUID("00000000-0000-0000-0000-000000000101")


class InMemoryPendingTaskModeStore(PendingTaskModeStore):
    def __init__(self) -> None:
        self.modes: dict[UUID, PendingCaptureMode] = {}
        self.created_tasks: list[Task] = []

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        self.modes[command.access_context.user_space_id] = (
            PendingCaptureMode.AWAITING_TASK_TEXT
        )

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        self.modes[command.access_context.user_space_id] = PendingCaptureMode.NORMAL

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        user_space_id = command.access_context.user_space_id
        if self.modes.get(user_space_id, PendingCaptureMode.NORMAL) is not (
            PendingCaptureMode.AWAITING_TASK_TEXT
        ):
            return None
        self.modes[user_space_id] = PendingCaptureMode.NORMAL
        task = Task(
            id=UUID("00000000-0000-0000-0000-000000000201"),
            user_space_id=user_space_id,
            title=command.text,
            description=None,
            status=TaskStatus.INBOX,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self.created_tasks.append(task)
        return task


def set_awaiting_command(access_context: AccessContext) -> SetAwaitingTaskCommand:
    return SetAwaitingTaskCommand(
        access_context=access_context,
        updated_at=NOW,
        trace_id="1" * 32,
    )


def cancel_command(access_context: AccessContext) -> CancelPendingTaskCommand:
    return CancelPendingTaskCommand(
        access_context=access_context,
        updated_at=NOW,
        trace_id="1" * 32,
    )


def text_command(
    *,
    access_context: AccessContext = ACCESS_A,
    text: str | None = "  Купить молоко  ",
    is_private_chat: bool = True,
    telegram_message_id: int | None = 501,
) -> ConsumePendingTaskTextCommand:
    return ConsumePendingTaskTextCommand(
        access_context=access_context,
        text=text,
        is_private_chat=is_private_chat,
        telegram_message_id=telegram_message_id,
        source_capture_event_id=SOURCE_A,
        created_at=NOW,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
async def test_set_awaiting_task_is_scoped_to_one_user_space() -> None:
    store = InMemoryPendingTaskModeStore()
    task_capture = TaskCapture(store)

    await task_capture.set_awaiting_task(set_awaiting_command(ACCESS_A))

    assert store.modes[ACCESS_A.user_space_id] is PendingCaptureMode.AWAITING_TASK_TEXT
    assert store.modes.get(ACCESS_B.user_space_id, PendingCaptureMode.NORMAL) is (
        PendingCaptureMode.NORMAL
    )


@pytest.mark.asyncio
async def test_cancel_returns_the_user_space_to_normal_mode() -> None:
    store = InMemoryPendingTaskModeStore()
    task_capture = TaskCapture(store)
    await task_capture.set_awaiting_task(set_awaiting_command(ACCESS_A))

    await task_capture.cancel(cancel_command(ACCESS_A))

    assert store.modes[ACCESS_A.user_space_id] is PendingCaptureMode.NORMAL
    assert store.created_tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "is_private_chat", "telegram_message_id"),
    [
        (None, True, 501),
        ("", True, 501),
        ("  /start", True, 501),
        ("ordinary group text", False, 501),
        ("text without message id", True, None),
    ],
)
async def test_ineligible_text_preserves_awaiting_mode(
    text: str | None, is_private_chat: bool, telegram_message_id: int | None
) -> None:
    store = InMemoryPendingTaskModeStore()
    task_capture = TaskCapture(store)
    await task_capture.set_awaiting_task(set_awaiting_command(ACCESS_A))

    task = await task_capture.consume_for_text(
        text_command(
            text=text,
            is_private_chat=is_private_chat,
            telegram_message_id=telegram_message_id,
        )
    )

    assert task is None
    assert store.modes[ACCESS_A.user_space_id] is PendingCaptureMode.AWAITING_TASK_TEXT
    assert store.created_tasks == []


@pytest.mark.asyncio
async def test_eligible_text_consumes_mode_and_creates_one_inbox_task() -> None:
    store = InMemoryPendingTaskModeStore()
    task_capture = TaskCapture(store)
    await task_capture.set_awaiting_task(set_awaiting_command(ACCESS_A))

    task = await task_capture.consume_for_text(text_command())

    assert task is not None
    assert task.title == "  Купить молоко  "
    assert task.description is None
    assert task.status is TaskStatus.INBOX
    assert task.user_space_id == ACCESS_A.user_space_id
    assert task.source_capture_event_id == SOURCE_A
    assert store.modes[ACCESS_A.user_space_id] is PendingCaptureMode.NORMAL
    assert store.created_tasks == [task]


@pytest.mark.asyncio
async def test_eligible_text_does_not_create_a_task_in_normal_mode() -> None:
    store = InMemoryPendingTaskModeStore()

    task = await TaskCapture(store).consume_for_text(text_command())

    assert task is None
    assert store.created_tasks == []

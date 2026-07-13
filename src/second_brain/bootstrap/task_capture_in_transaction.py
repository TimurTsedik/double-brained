from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventWriter,
)
from second_brain.slices.capture.application.capture_text import CaptureText
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    CaptureTextPort,
)
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import UpdateTransaction
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresPendingTaskModeWriter,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingTaskTextCommand,
    SetAwaitingTaskCommand,
    TaskModePort,
)
from second_brain.slices.tasks.application.task_capture import TaskCapture


class TaskCaptureInTransaction(CaptureTextPort, TaskModePort):
    """Bootstrap-only composition for receipt, source, task, and mode writes."""

    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent:
        session = _active_session(transaction)
        source = await CaptureText(PostgresCaptureEventWriter(session)).execute(command)
        await TaskCapture(PostgresPendingTaskModeWriter(session)).consume_for_text(
            ConsumePendingTaskTextCommand(
                access_context=command.access_context,
                text=command.raw_text,
                is_private_chat=True,
                telegram_message_id=command.telegram_message_id,
                source_capture_event_id=source.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        return source

    async def set_awaiting_task(
        self, command: SetAwaitingTaskCommand, transaction: UpdateTransaction
    ) -> None:
        task_capture = TaskCapture(
            PostgresPendingTaskModeWriter(_active_session(transaction))
        )
        await task_capture.set_awaiting_task(command)

    async def cancel(
        self, command: CancelPendingTaskCommand, transaction: UpdateTransaction
    ) -> None:
        task_capture = TaskCapture(
            PostgresPendingTaskModeWriter(_active_session(transaction))
        )
        await task_capture.cancel(command)


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("task capture requires the PostgreSQL update transaction")
    return transaction.active_session

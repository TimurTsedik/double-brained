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
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.knowledge.adapters.persistence.repository import (
    PostgresKnowledgeWriter,
)
from second_brain.slices.knowledge.domain.entities import Decision, Idea, Note, Question
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CreateTextProcessingRunCommand,
)
from second_brain.slices.processing.domain.entities import TranscriptionOutputType
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresPendingCaptureSelectionWriter,
    PostgresTaskPanelWriter,
    PostgresTaskWriter,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    CompleteTaskCommand,
    ConsumePendingTaskTextCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
    TaskModePort,
    TaskPanelPort,
    TaskPanelResult,
)
from second_brain.slices.tasks.application.task_capture import TaskCapture
from second_brain.slices.tasks.application.task_panel import TaskPanel
from second_brain.slices.tasks.domain.entities import Task


class TaskCaptureInTransaction(CaptureTextPort, TaskModePort, TaskPanelPort):
    """Bootstrap-only composition for receipt, source, task, and mode writes."""

    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent:
        session = _active_session(transaction)
        source = await CaptureText(PostgresCaptureEventWriter(session)).execute(command)
        task_capture = _typed_task_capture(session)
        record = await task_capture.consume_for_text(
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
        if record is not None:
            await PostgresProcessingWriter(session).create_text_run(
                CreateTextProcessingRunCommand(
                    access_context=command.access_context,
                    capture_event_id=source.id,
                    output_type=_record_output_type(record),
                    created_at=command.received_at,
                    trace_id=command.trace_id,
                )
            )
        return source

    async def set_awaiting_task(
        self, command: SetAwaitingTaskCommand, transaction: UpdateTransaction
    ) -> None:
        task_capture = _typed_task_capture(_active_session(transaction))
        await task_capture.set_awaiting_task(command)

    async def set_selection(
        self, command: SetPendingCaptureSelectionCommand, transaction: UpdateTransaction
    ) -> None:
        await _typed_task_capture(_active_session(transaction)).set_selection(command)

    async def cancel(
        self, command: CancelPendingTaskCommand, transaction: UpdateTransaction
    ) -> None:
        task_capture = _typed_task_capture(_active_session(transaction))
        await task_capture.cancel(command)

    async def list_open(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> TaskPanelResult:
        return await TaskPanel(
            PostgresTaskPanelWriter(_active_session(transaction))
        ).list_open(access_context)

    async def complete(
        self, command: CompleteTaskCommand, transaction: UpdateTransaction
    ) -> TaskPanelResult:
        return await TaskPanel(
            PostgresTaskPanelWriter(_active_session(transaction))
        ).complete(command)


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("task capture requires the PostgreSQL update transaction")
    return transaction.active_session


def _typed_task_capture(session: AsyncSession) -> TaskCapture:
    return TaskCapture(
        PostgresPendingCaptureSelectionWriter(session),
        PostgresTaskWriter(session),
        PostgresKnowledgeWriter(session),
    )


def _record_output_type(
    record: Task | Note | Idea | Decision | Question,
) -> TranscriptionOutputType:
    if isinstance(record, Task):
        return TranscriptionOutputType.TASK
    if isinstance(record, Note):
        return TranscriptionOutputType.NOTE
    if isinstance(record, Idea):
        return TranscriptionOutputType.IDEA
    if isinstance(record, Decision):
        return TranscriptionOutputType.DECISION
    return TranscriptionOutputType.QUESTION

from collections.abc import Callable
from datetime import datetime

from second_brain.slices.knowledge.application.contracts import (
    CreateDecisionCommand,
    CreateIdeaCommand,
    CreateNoteCommand,
    CreateQuestionCommand,
    KnowledgeCapturePort,
    KnowledgeRecord,
)
from second_brain.slices.reminders.application.contracts import (
    CreateReminderCommand,
    ReminderWriter,
    SpaceTimezoneReader,
    TimeExtractor,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingCaptureSelectionCommand,
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    CreateTypedCaptureCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType, Task
from second_brain.slices.tasks.ports.repositories import (
    PendingCaptureSelectionStore,
    TaskWriter,
)


class TaskCapture:
    def __init__(
        self,
        pending_capture_selection_store: PendingCaptureSelectionStore,
        task_writer: TaskWriter | None = None,
        knowledge_capture: KnowledgeCapturePort | None = None,
        reminder_writer: ReminderWriter | None = None,
        time_extractor: TimeExtractor | None = None,
        timezone_reader: SpaceTimezoneReader | None = None,
        on_reminder_created: Callable[[datetime, str], None] | None = None,
    ) -> None:
        if (task_writer is None) != (knowledge_capture is None):
            raise ValueError("typed task capture requires both writers")
        self._pending_capture_selection_store = pending_capture_selection_store
        self._task_writer = task_writer
        self._knowledge_capture = knowledge_capture
        self._reminder_writer = reminder_writer
        self._time_extractor = time_extractor
        self._timezone_reader = timezone_reader
        # Слушатель «напоминание создано» (remind_at UTC, tz пространства) —
        # его задают воркер-пути (классификация/голос), чтобы подтвердить
        # «⏰ Напомню…» ПОСЛЕ коммита; кнопочный путь подтверждает poller-ack'ом
        # и слушателя не передаёт — двойного подтверждения нет.
        self._on_reminder_created = on_reminder_created

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        await self._pending_capture_selection_store.set_awaiting_task(command)

    async def set_selection(self, command: SetPendingCaptureSelectionCommand) -> None:
        await self._pending_capture_selection_store.set_selection(command)

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        await self._pending_capture_selection_store.cancel(command)

    async def consume_selection(
        self, command: ConsumePendingCaptureSelectionCommand
    ) -> PendingCaptureType:
        return await self._pending_capture_selection_store.consume_selection(command)

    async def consume_for_text(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | KnowledgeRecord | None:
        if not _is_eligible(command):
            return None
        if self._task_writer is None and self._knowledge_capture is None:
            return await self._pending_capture_selection_store.consume_awaiting_task(
                command
            )
        if self._task_writer is None or self._knowledge_capture is None:
            raise RuntimeError("typed task capture writers are incomplete")
        selection = await self._pending_capture_selection_store.consume_selection(
            ConsumePendingCaptureSelectionCommand(
                access_context=command.access_context,
                consumed_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        if command.text is None:
            raise ValueError("eligible typed capture text must not be None")
        return await self.create_for_selection(
            CreateTypedCaptureCommand(
                access_context=command.access_context,
                selection=selection,
                text=command.text,
                source_capture_event_id=command.source_capture_event_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )

    async def create_for_selection(
        self, command: CreateTypedCaptureCommand
    ) -> Task | KnowledgeRecord:
        if self._task_writer is None or self._knowledge_capture is None:
            raise RuntimeError("typed task capture writers are incomplete")
        if command.selection is PendingCaptureType.TASK:
            task = await self._task_writer.create(
                CreateTaskCommand(
                    access_context=command.access_context,
                    title=command.text,
                    source_capture_event_id=command.source_capture_event_id,
                    created_at=command.created_at,
                    trace_id=command.trace_id,
                )
            )
            await self._maybe_create_reminder(command, task)
            return task
        if command.selection is PendingCaptureType.NOTE:
            return await self._knowledge_capture.create_note(
                CreateNoteCommand(
                    access_context=command.access_context,
                    text=command.text,
                    source_capture_event_id=command.source_capture_event_id,
                    created_at=command.created_at,
                    trace_id=command.trace_id,
                )
            )
        if command.selection is PendingCaptureType.IDEA:
            return await self._knowledge_capture.create_idea(
                CreateIdeaCommand(
                    access_context=command.access_context,
                    text=command.text,
                    source_capture_event_id=command.source_capture_event_id,
                    created_at=command.created_at,
                    trace_id=command.trace_id,
                )
            )
        if command.selection is PendingCaptureType.DECISION:
            return await self._knowledge_capture.create_decision(
                CreateDecisionCommand(
                    access_context=command.access_context,
                    text=command.text,
                    source_capture_event_id=command.source_capture_event_id,
                    created_at=command.created_at,
                    trace_id=command.trace_id,
                )
            )
        return await self._knowledge_capture.create_question(
            CreateQuestionCommand(
                access_context=command.access_context,
                text=command.text,
                source_capture_event_id=command.source_capture_event_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )

    async def _maybe_create_reminder(
        self, command: CreateTypedCaptureCommand, task: Task
    ) -> None:
        # Единая точка (оба пути — текст-капча и авто-классификация в task):
        # если в тексте задачи есть явный БУДУЩИЙ момент, ставим напоминание.
        # Заголовок задачи НЕ переписываем. Прошлое/без времени → без напоминания.
        if (
            self._reminder_writer is None
            or self._time_extractor is None
            or self._timezone_reader is None
        ):
            return
        tz = await self._timezone_reader.resolve_timezone(command.access_context)
        remind_at = self._time_extractor.extract_due(
            command.text, command.created_at, tz
        )
        if remind_at is None:
            return
        await self._reminder_writer.create_reminder(
            CreateReminderCommand(
                access_context=command.access_context,
                remind_at=remind_at,
                text=command.text,
                source_task_id=task.id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        if self._on_reminder_created is not None:
            self._on_reminder_created(remind_at, tz)


def _is_eligible(command: ConsumePendingTaskTextCommand) -> bool:
    return (
        command.is_private_chat
        and command.text is not None
        and command.text != ""
        and command.telegram_message_id is not None
        and not command.text.lstrip().startswith("/")
    )

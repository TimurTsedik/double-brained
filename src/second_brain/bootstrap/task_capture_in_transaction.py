from collections.abc import Callable, Sequence
from datetime import datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, text
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
    read_user_space_timezone,
)
from second_brain.slices.identity.adapters.telegram.gateway import (
    REMINDER_WHEN_FORMAT,
)
from second_brain.slices.identity.adapters.telegram.messages import reminder_set_text
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
    WorkerIdentityPort,
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
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkWriter,
)
from second_brain.slices.projects.application.contracts import (
    InheritCaptureProjectLinksCommand,
    LinkCurrentProjectToCaptureCommand,
)
from second_brain.slices.projects.domain.entities import ProjectContentKind
from second_brain.slices.reminders.adapters.dateparser.extractor import (
    DateparserTimeExtractor,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.adapters.persistence.repository import (
    PostgresReminderWriter,
)
from second_brain.slices.reminders.application.contracts import (
    DEFAULT_TIMEZONE,
    CancelReminderForTaskCommand,
    ReminderAckReader,
    ReminderDeliveryPort,
)
from second_brain.slices.reminders.domain.entities import ReminderStatus
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
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
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresWeblinkWriter,
)
from second_brain.slices.weblinks.application.contracts import (
    RecordUrlEntry,
    SaveRecordLinksCommand,
    WeblinkRecordKind,
)


class TaskCaptureInTransaction(
    CaptureTextPort, TaskModePort, TaskPanelPort, ReminderAckReader
):
    """Bootstrap-only composition for receipt, source, task, and mode writes."""

    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent:
        session = _active_session(transaction)
        source = await CaptureText(PostgresCaptureEventWriter(session)).execute(command)
        project_links = PostgresProjectContentLinkWriter(session)
        await project_links.link_current_to_capture(
            LinkCurrentProjectToCaptureCommand(
                access_context=command.access_context,
                capture_event_id=source.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
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
            await project_links.inherit_capture_links(
                InheritCaptureProjectLinksCommand(
                    access_context=command.access_context,
                    source_capture_event_id=source.id,
                    content_kind=record_project_kind(record),
                    content_id=record.id,
                    created_at=command.received_at,
                    trace_id=command.trace_id,
                )
            )
            run = await PostgresProcessingWriter(session).create_text_run(
                CreateTextProcessingRunCommand(
                    access_context=command.access_context,
                    capture_event_id=source.id,
                    output_type=record_output_type(record),
                    created_at=command.received_at,
                    trace_id=command.trace_id,
                )
            )
            await PostgresSemanticIndexWriter(session).register_target(
                RegisterIndexingTargetCommand(
                    access_context=command.access_context,
                    processing_run_id=run.id,
                    record_kind=SearchRecordType(record_output_type(record).value),
                    record_id=record.id,
                    created_at=command.received_at,
                    trace_id=command.trace_id,
                )
            )
            # Sidecar-ссылки — тем же коммитом, с видом/id ФАКТИЧЕСКОЙ записи
            # (текст записи дословный, пары «слово → адрес» живут рядом).
            # Запись не создана (не eligible) — ссылки не пишутся: ветка выше.
            if command.links:
                await PostgresWeblinkWriter(session).save_links(
                    SaveRecordLinksCommand(
                        access_context=command.access_context,
                        record_kind=record_weblink_kind(record),
                        record_id=record.id,
                        entries=tuple(
                            RecordUrlEntry(label=link.label, url=link.url)
                            for link in command.links
                        ),
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
        session = _active_session(transaction)
        result = await TaskPanel(PostgresTaskPanelWriter(session)).complete(command)
        # Хук завершения: сделанная задача больше не пингует — её ещё pending-
        # напоминание гасим (sent/cancelled не трогаем; нет напоминания — no-op).
        await PostgresReminderWriter(session).cancel_for_task(
            CancelReminderForTaskCommand(
                access_context=command.access_context,
                source_task_id=command.task_id,
                cancelled_at=command.completed_at,
            )
        )
        return result

    async def reminder_for_capture(
        self,
        access_context: AccessContext,
        capture_event_id: UUID,
        transaction: UpdateTransaction,
    ) -> datetime | None:
        # Ack-канал: «на когда» напоминание, поставленное задачей из ЭТОЙ капчи.
        # Возвращаем момент в часовом поясе пространства — ack показывает его.
        session = _active_session(transaction)
        remind_at = await _read_capture_reminder_at(
            session, access_context, capture_event_id
        )
        if remind_at is None:
            return None
        timezone = await PostgresSpaceTimezoneReader(session).resolve_timezone(
            access_context
        )
        return remind_at.astimezone(ZoneInfo(timezone))


async def _read_capture_reminder_at(
    session: AsyncSession,
    access_context: AccessContext,
    capture_event_id: UUID,
) -> datetime | None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )
    return cast(
        datetime | None,
        await session.scalar(
            select(ReminderModel.remind_at)
            .join(
                TaskModel,
                and_(
                    TaskModel.id == ReminderModel.source_task_id,
                    TaskModel.user_space_id == ReminderModel.user_space_id,
                ),
            )
            .where(
                TaskModel.source_capture_event_id == capture_event_id,
                ReminderModel.user_space_id == access_context.user_space_id,
                ReminderModel.status == ReminderStatus.PENDING,
            )
        ),
    )


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("task capture requires the PostgreSQL update transaction")
    return transaction.active_session


class PostgresSpaceTimezoneReader:
    """Resolves a space's timezone on a caller-owned session for the capture flow."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_timezone(self, access_context: AccessContext) -> str:
        timezone = await read_user_space_timezone(
            self._session,
            access_context.user_space_id,
            access_context.user_id,
        )
        return timezone or DEFAULT_TIMEZONE


def build_task_capture(
    session: AsyncSession,
    on_reminder_created: Callable[[datetime, str], None] | None = None,
) -> TaskCapture:
    """Reminder-enabled TaskCapture over a caller-owned session.

    One construction point for every task-creation path (typed text capture,
    auto-classification, voice transcription) so a due time typed into the task
    always yields a reminder — the single creation point stays single.

    ``on_reminder_created`` задают ТОЛЬКО воркер-пути (классификация/голос) —
    там некому ответить в чате, и подтверждение «⏰ Напомню…» шлёт сам воркер
    после коммита. Кнопочный путь подтверждает существующим poller-ack'ом и
    слушателя не передаёт — двойного подтверждения не бывает.
    """
    return TaskCapture(
        PostgresPendingCaptureSelectionWriter(session),
        PostgresTaskWriter(session),
        PostgresKnowledgeWriter(session),
        reminder_writer=PostgresReminderWriter(session),
        time_extractor=DateparserTimeExtractor(),
        timezone_reader=PostgresSpaceTimezoneReader(session),
        on_reminder_created=on_reminder_created,
    )


async def send_reminder_confirmations(
    delivery_port: ReminderDeliveryPort,
    identity: WorkerIdentityPort,
    access_context: AccessContext,
    confirmations: Sequence[tuple[datetime, str]],
) -> None:
    """Подтверждение «⏰ Напомню {when}» для напоминаний, созданных воркером.

    Тот же локализованный текст и формат времени, что и в poller-ack'е
    (``reminder.set`` + ``REMINDER_WHEN_FORMAT``), момент — в часовом поясе
    пространства. Вызывать строго ПОСЛЕ коммита создающей транзакции:
    осознанный lean-край — сбой между коммитом и отправкой теряет/дублирует
    только подтверждение, само напоминание уже надёжно в базе.
    """
    if not confirmations:
        return
    locale = await identity.resolve_locale(access_context)
    recipient = await identity.resolve_telegram_recipient(access_context)
    for remind_at, timezone in confirmations:
        when = remind_at.astimezone(ZoneInfo(timezone)).strftime(REMINDER_WHEN_FORMAT)
        await delivery_port.deliver(reminder_set_text(when, locale), recipient)


def _typed_task_capture(session: AsyncSession) -> TaskCapture:
    return build_task_capture(session)


def record_output_type(
    record: Task | Note | Idea | Decision | Question,
) -> TranscriptionOutputType:
    """Тип обработки по ФАКТИЧЕСКИ созданной записи (для голоса, где текст со
    временем мог быть маршрутизирован NOTE→TASK — индексация и метка
    уведомления идут за фактом, а не за замороженным типом)."""
    if isinstance(record, Task):
        return TranscriptionOutputType.TASK
    if isinstance(record, Note):
        return TranscriptionOutputType.NOTE
    if isinstance(record, Idea):
        return TranscriptionOutputType.IDEA
    if isinstance(record, Decision):
        return TranscriptionOutputType.DECISION
    return TranscriptionOutputType.QUESTION


def record_project_kind(
    record: Task | Note | Idea | Decision | Question,
) -> ProjectContentKind:
    """Вид записи для проектных связей — по ФАКТИЧЕСКОЙ записи (см.
    ``record_output_type``)."""
    if isinstance(record, Task):
        return ProjectContentKind.TASK
    if isinstance(record, Note):
        return ProjectContentKind.NOTE
    if isinstance(record, Idea):
        return ProjectContentKind.IDEA
    if isinstance(record, Decision):
        return ProjectContentKind.DECISION
    return ProjectContentKind.QUESTION


def record_weblink_kind(
    record: Task | Note | Idea | Decision | Question,
) -> WeblinkRecordKind:
    """Вид записи для sidecar-ссылок — по ФАКТИЧЕСКОЙ записи (см.
    ``record_output_type``: NOTE со временем мог стать TASK)."""
    return WeblinkRecordKind(record_output_type(record).value)

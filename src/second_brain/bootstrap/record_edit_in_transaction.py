"""Bootstrap-композиция правки записи внутри update-транзакции (S3, спека §3).

Собирает за портом RecordEditPort все последствия правки ОДНИМ коммитом:
UPDATE текста записи → INDEXING-only прогон (пере-индексация БЕЗ
пере-классификации) → пересбор sidecar-ссылок под новый текст. Напоминание
задачи НЕ трогается (решение владельца §6.2) — только строка
«⏰ напоминание осталось…» в подтверждении.
"""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.bootstrap.task_capture_in_transaction import (
    PostgresSpaceTimezoneReader,
)
from second_brain.slices.editing.adapters.persistence.repository import (
    LockedPendingEdit,
    PostgresPendingEditWriter,
)
from second_brain.slices.editing.application.contracts import (
    BeginRecordEditCommand,
    ConsumeRecordEditCommand,
    RecordEditPort,
    RecordEditResult,
)
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
from second_brain.slices.knowledge.application.contracts import (
    KnowledgeRecordKind,
    UpdateKnowledgeTextCommand,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CreateIndexProcessingRunCommand,
)
from second_brain.slices.processing.domain.entities import TranscriptionOutputType
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.domain.entities import ReminderStatus
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresRecordViewReader,
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
    SearchRecordType,
)
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresTaskWriter,
)
from second_brain.slices.tasks.application.contracts import RenameTaskCommand
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresWeblinkWriter,
)
from second_brain.slices.weblinks.application.contracts import (
    RecordUrlEntry,
    SaveRecordLinksCommand,
    WeblinkRecordKind,
)


class RecordEditInTransaction(RecordEditPort):
    """Правка записи: pending-режим + все последствия в транзакции вызывающего."""

    async def begin(
        self, command: BeginRecordEditCommand, transaction: UpdateTransaction
    ) -> bool:
        session = _active_session(transaction)
        # Владение и существование — ПРИ УСТАНОВКЕ режима: чтение записи под
        # forced RLS + same-space предикатом (тот же ридер, что у показа).
        record = await PostgresRecordViewReader(session).read_record(
            command.access_context, command.record_kind, command.record_id
        )
        if record is None:
            return False
        await PostgresPendingEditWriter(session).set_pending(command)
        return True

    async def cancel(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> None:
        await PostgresPendingEditWriter(_active_session(transaction)).cancel(
            access_context
        )

    async def consume_new_text(
        self, command: ConsumeRecordEditCommand, transaction: UpdateTransaction
    ) -> RecordEditResult | None:
        session = _active_session(transaction)
        pending_writer = PostgresPendingEditWriter(session)
        pending = await pending_writer.lock_pending(command.access_context)
        if pending is None:
            return None
        if command.text.strip() == "":
            # Пробельный «новый текст» — не правка: запись/ссылки/индекс не
            # трогаем, режим НЕ потребляем — ждём настоящий текст. (Валидный
            # текст ниже сохраняется ДОСЛОВНО, без стрипа.)
            return RecordEditResult(
                record_kind=pending.record_kind,
                record_id=pending.record_id,
                text_required=True,
            )
        # Режим одноразовый: потребляется в любом исходе (row-lock выше
        # сериализует два быстрых сообщения — правку применит ровно одно).
        await pending_writer.cancel(command.access_context)
        source_capture_event_id = await _update_record_text(session, command, pending)
        if source_capture_event_id is None:
            # Повторная проверка владения/существования не прошла.
            return RecordEditResult(
                record_kind=pending.record_kind,
                record_id=pending.record_id,
                record_missing=True,
            )
        # Пере-индексация БЕЗ пере-классификации: INDEXING-only прогон, цель —
        # правленая запись (воркер прочтёт её ТЕКУЩИЙ текст и заменит чанки).
        run = await PostgresProcessingWriter(session).create_index_run(
            CreateIndexProcessingRunCommand(
                access_context=command.access_context,
                capture_event_id=source_capture_event_id,
                output_type=TranscriptionOutputType(pending.record_kind.value),
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        await PostgresSemanticIndexWriter(session).register_target(
            RegisterIndexingTargetCommand(
                access_context=command.access_context,
                processing_run_id=run.id,
                record_kind=pending.record_kind,
                record_id=pending.record_id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        # Sidecar-ссылки отражают НОВЫЙ текст: замена набора тем же коммитом
        # (нет ссылок в новом тексте → набор станет пустым).
        await PostgresWeblinkWriter(session).replace_links(
            SaveRecordLinksCommand(
                access_context=command.access_context,
                record_kind=WeblinkRecordKind(pending.record_kind.value),
                record_id=pending.record_id,
                entries=tuple(
                    RecordUrlEntry(label=link.label, url=link.url)
                    for link in command.links
                ),
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        reminder_when = None
        if pending.record_kind is SearchRecordType.TASK:
            reminder_when = await _pending_reminder_when(
                session, command.access_context, pending.record_id
            )
        return RecordEditResult(
            record_kind=pending.record_kind,
            record_id=pending.record_id,
            reminder_when=reminder_when,
        )


async def _update_record_text(
    session: AsyncSession,
    command: ConsumeRecordEditCommand,
    pending: LockedPendingEdit,
) -> UUID | None:
    """UPDATE текста записи в её таблице; возвращает source_capture_event_id."""
    if pending.record_kind is SearchRecordType.TASK:
        return await PostgresTaskWriter(session).rename(
            RenameTaskCommand(
                access_context=command.access_context,
                task_id=pending.record_id,
                title=command.text,
                updated_at=command.received_at,
            )
        )
    return await PostgresKnowledgeWriter(session).update_text(
        UpdateKnowledgeTextCommand(
            access_context=command.access_context,
            record_kind=KnowledgeRecordKind(pending.record_kind.value),
            record_id=pending.record_id,
            text=command.text,
            updated_at=command.received_at,
        )
    )


async def _pending_reminder_when(
    session: AsyncSession, access_context: AccessContext, task_id: UUID
) -> datetime | None:
    """Живое (pending) напоминание задачи — момент в tz пространства.

    Само напоминание правкой НЕ трогается: читаем только «на когда», чтобы
    подтверждение показало «⏰ напоминание осталось на …».
    """
    remind_at = await session.scalar(
        select(ReminderModel.remind_at).where(
            ReminderModel.source_task_id == task_id,
            ReminderModel.user_space_id == access_context.user_space_id,
            ReminderModel.status == ReminderStatus.PENDING,
        )
    )
    if remind_at is None:
        return None
    timezone = await PostgresSpaceTimezoneReader(session).resolve_timezone(
        access_context
    )
    return remind_at.astimezone(ZoneInfo(timezone))


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("record edit requires the PostgreSQL update transaction")
    return transaction.active_session

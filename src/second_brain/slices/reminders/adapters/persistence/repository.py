from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.application.contracts import (
    CancelReminderForTaskCommand,
    ClaimedReminder,
    CreateReminderCommand,
)
from second_brain.slices.reminders.domain.entities import Reminder, ReminderStatus

# Бюджет доставки: после MAX_SEND_ATTEMPTS неудачных отправок напоминание
# переводится в failed и больше не claim'ится; между попытками — линейный
# бэкофф attempts × BACKOFF_STEP (без lease-механики — осознанно минимально).
MAX_SEND_ATTEMPTS = 5
BACKOFF_STEP = timedelta(seconds=60)


class PostgresReminderWriter:
    """Reminder reads/writes through a transaction owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_reminder(self, command: CreateReminderCommand) -> Reminder:
        await _set_user_space_scope(self._session, command.access_context)
        model = ReminderModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            remind_at=command.remind_at,
            text=command.text,
            status=ReminderStatus.PENDING,
            source_task_id=command.source_task_id,
            send_attempts=0,
            next_attempt_at=command.remind_at,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_entity(model)

    async def cancel_for_task(self, command: CancelReminderForTaskCommand) -> None:
        # Завершили задачу → её ещё не отправленное напоминание гасим (о сделанном
        # не пингуем). sent/cancelled не трогаем: только pending → cancelled.
        await _set_user_space_scope(self._session, command.access_context)
        await self._session.execute(
            update(ReminderModel)
            .where(
                ReminderModel.source_task_id == command.source_task_id,
                ReminderModel.user_space_id == command.access_context.user_space_id,
                ReminderModel.status == ReminderStatus.PENDING,
            )
            .values(
                status=ReminderStatus.CANCELLED,
                updated_at=command.cancelled_at,
            )
        )
        await self._session.flush()

    async def claim_due(
        self, access_context: AccessContext, now: datetime
    ) -> ClaimedReminder | None:
        # Захват ОДНОЙ созревшей строки под блокировкой (M1/M5): FOR UPDATE SKIP
        # LOCKED, чтобы перекрывающиеся тики брали РАЗНЫЕ строки, а не одну дважды.
        # «Пора» = next_attempt_at (учитывает бэкофф); remind_at — только время
        # пользователя для показа.
        await _set_user_space_scope(self._session, access_context)
        claimed = await self._session.scalar(
            select(ReminderModel)
            .where(
                ReminderModel.user_space_id == access_context.user_space_id,
                ReminderModel.status == ReminderStatus.PENDING,
                ReminderModel.next_attempt_at <= now,
            )
            .order_by(ReminderModel.next_attempt_at, ReminderModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if claimed is None:
            return None
        return ClaimedReminder(
            reminder_id=claimed.id,
            text=claimed.text,
            trace_id=claimed.trace_id,
        )

    async def record_send_failure(
        self, access_context: AccessContext, reminder_id: UUID, now: datetime
    ) -> None:
        # Учёт неудачной отправки: попытка += 1; бюджет исчерпан → failed
        # (больше никогда не claim'ится), иначе линейный бэкофф — чтобы вечный
        # сбой не долбил Telegram каждый тик и не голодил соседние напоминания.
        await _set_user_space_scope(self._session, access_context)
        failed = await self._session.scalar(
            select(ReminderModel)
            .where(
                ReminderModel.id == reminder_id,
                ReminderModel.user_space_id == access_context.user_space_id,
                ReminderModel.status == ReminderStatus.PENDING,
            )
            .with_for_update()
        )
        if failed is None:
            return
        failed.send_attempts += 1
        if failed.send_attempts >= MAX_SEND_ATTEMPTS:
            failed.status = ReminderStatus.FAILED
        else:
            failed.next_attempt_at = now + failed.send_attempts * BACKOFF_STEP
        failed.updated_at = now
        await self._session.flush()

    async def mark_sent(
        self, access_context: AccessContext, reminder_id: UUID, sent_at: datetime
    ) -> bool:
        # Идемпотентный переход pending → sent: RETURNING-guard, повтор/второй
        # тик по уже отправленной строке ничего не меняет (rowcount 0 → False).
        await _set_user_space_scope(self._session, access_context)
        sent_id = await self._session.scalar(
            update(ReminderModel)
            .where(
                ReminderModel.id == reminder_id,
                ReminderModel.user_space_id == access_context.user_space_id,
                ReminderModel.status == ReminderStatus.PENDING,
            )
            .values(status=ReminderStatus.SENT, updated_at=sent_at)
            .returning(ReminderModel.id)
        )
        await self._session.flush()
        return sent_id is not None


class PostgresReminderRepository:
    """Owns the session/transaction for one-shot reminder operations."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_reminder(self, command: CreateReminderCommand) -> Reminder:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresReminderWriter(session).create_reminder(command)

    async def cancel_for_task(self, command: CancelReminderForTaskCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresReminderWriter(session).cancel_for_task(command)


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_entity(model: ReminderModel) -> Reminder:
    return Reminder(
        id=model.id,
        user_space_id=model.user_space_id,
        remind_at=model.remind_at,
        text=model.text,
        status=model.status,
        source_task_id=model.source_task_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
        trace_id=model.trace_id,
    )

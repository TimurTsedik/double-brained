"""Postgres-адаптер pending-режима правки: set/cancel/lock-consume.

Паттерн — как у остальных pending-режимов: установка upsert'ом, потребление
под row-lock (`FOR UPDATE`), чтобы два быстрых сообщения после «✏️ Править»
сериализовались и правку применило ровно одно из них.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.slices.editing.adapters.persistence.models import (
    PendingEditModeModel,
)
from second_brain.slices.editing.application.contracts import BeginRecordEditCommand
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import SearchRecordType


@dataclass(frozen=True)
class LockedPendingEdit:
    """Захваченный row-lock'ом pending-режим: какая запись ждёт новый текст."""

    record_kind: SearchRecordType
    record_id: UUID = field(repr=False)


class PostgresPendingEditWriter:
    """Владеет строкой pending_edit_modes в транзакции вызывающего."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_pending(self, command: BeginRecordEditCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        statement = (
            postgresql_insert(PendingEditModeModel)
            .values(
                user_space_id=command.access_context.user_space_id,
                record_kind=command.record_kind,
                record_id=command.record_id,
                updated_at=command.updated_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_update(
                index_elements=[PendingEditModeModel.user_space_id],
                set_={
                    "record_kind": command.record_kind,
                    "record_id": command.record_id,
                    "updated_at": command.updated_at,
                    "trace_id": command.trace_id,
                },
            )
        )
        await self._session.execute(statement)

    async def cancel(self, access_context: AccessContext) -> None:
        await _set_user_space_scope(self._session, access_context)
        await self._session.execute(
            delete(PendingEditModeModel).where(
                PendingEditModeModel.user_space_id == access_context.user_space_id
            )
        )

    async def lock_pending(
        self, access_context: AccessContext
    ) -> LockedPendingEdit | None:
        await _set_user_space_scope(self._session, access_context)
        pending = await self._session.scalar(
            select(PendingEditModeModel)
            .where(PendingEditModeModel.user_space_id == access_context.user_space_id)
            .with_for_update()
        )
        if pending is None:
            return None
        return LockedPendingEdit(
            record_kind=pending.record_kind, record_id=pending.record_id
        )


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

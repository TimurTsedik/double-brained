from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingTaskModeModel,
    TaskModel,
    TaskProvenanceModel,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.domain.entities import (
    PendingCaptureMode,
    Task,
    TaskStatus,
)


class PostgresTaskRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, command: CreateTaskCommand) -> Task:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresTaskWriter(session).create(command)


class PostgresTaskWriter:
    """Writes a Task and its provenance through a transaction owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, command: CreateTaskCommand) -> Task:
        await _set_user_space_scope(self._session, command.access_context)
        task_id = uuid4()
        model = TaskModel(
            id=task_id,
            user_space_id=command.access_context.user_space_id,
            title=command.title,
            description=None,
            status=TaskStatus.INBOX,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(
            TaskProvenanceModel(
                task_id=task_id,
                source_capture_event_id=command.source_capture_event_id,
                user_space_id=command.access_context.user_space_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        await self._session.flush()
        return _to_entity(model)


class PostgresPendingTaskModeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresPendingTaskModeWriter(session).set_awaiting_task(command)

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresPendingTaskModeWriter(session).cancel(command)

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresPendingTaskModeWriter(
                    session
                ).consume_awaiting_task(command)


class PostgresPendingTaskModeWriter:
    """Mutates pending state and creates Tasks through a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        mode = await self._get_or_create(
            command.access_context, command.updated_at, command.trace_id
        )
        mode.mode = PendingCaptureMode.AWAITING_TASK_TEXT
        mode.updated_at = command.updated_at
        mode.trace_id = command.trace_id
        await self._session.flush()

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        mode = await self._get_or_create(
            command.access_context, command.updated_at, command.trace_id
        )
        mode.mode = PendingCaptureMode.NORMAL
        mode.updated_at = command.updated_at
        mode.trace_id = command.trace_id
        await self._session.flush()

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        await _set_user_space_scope(self._session, command.access_context)
        mode = await self._session.scalar(
            select(PendingTaskModeModel)
            .where(
                PendingTaskModeModel.user_space_id
                == command.access_context.user_space_id
            )
            .with_for_update()
        )
        if mode is None or mode.mode is not PendingCaptureMode.AWAITING_TASK_TEXT:
            return None

        if command.text is None:
            raise ValueError("eligible task text must not be None")
        task = await PostgresTaskWriter(self._session).create(
            CreateTaskCommand(
                access_context=command.access_context,
                title=command.text,
                source_capture_event_id=command.source_capture_event_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        mode.mode = PendingCaptureMode.NORMAL
        mode.updated_at = command.created_at
        mode.trace_id = command.trace_id
        await self._session.flush()
        return task

    async def _get_or_create(
        self, access_context: AccessContext, updated_at: datetime, trace_id: str
    ) -> PendingTaskModeModel:
        await _set_user_space_scope(self._session, access_context)
        mode = await self._session.scalar(
            select(PendingTaskModeModel)
            .where(PendingTaskModeModel.user_space_id == access_context.user_space_id)
            .with_for_update()
        )
        if mode is not None:
            return mode

        mode = PendingTaskModeModel(
            user_space_id=access_context.user_space_id,
            mode=PendingCaptureMode.NORMAL,
            updated_at=updated_at,
            trace_id=trace_id,
        )
        self._session.add(mode)
        await self._session.flush()
        return mode


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_entity(model: TaskModel) -> Task:
    return Task(
        id=model.id,
        user_space_id=model.user_space_id,
        title=model.title,
        description=model.description,
        status=model.status,
        source_capture_event_id=model.source_capture_event_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
        trace_id=model.trace_id,
    )

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
    TaskProvenanceModel,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    CompleteTaskCommand,
    ConsumePendingCaptureSelectionCommand,
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    RenameTaskCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
)
from second_brain.slices.tasks.domain.entities import (
    PendingCaptureType,
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

    async def rename(self, command: RenameTaskCommand) -> UUID | None:
        """Правка (S3): заменить title + бампнуть updated_at и edited_at
        строго в СВОЁМ пространстве. Статус и напоминание не трогаются;
        edited_at ставится ТОЛЬКО здесь — по нему показ метит «(изменено)».

        Возвращает source_capture_event_id правленой задачи (нужен
        пере-индексации) или None, если задачи нет / она чужая.
        """
        await _set_user_space_scope(self._session, command.access_context)
        return await self._session.scalar(
            update(TaskModel)
            .where(
                TaskModel.id == command.task_id,
                TaskModel.user_space_id == command.access_context.user_space_id,
            )
            .values(
                title=command.title,
                updated_at=command.updated_at,
                edited_at=command.updated_at,
            )
            .returning(TaskModel.source_capture_event_id)
        )


class PostgresTaskPanelRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_inbox(
        self, access_context: AccessContext, limit: int
    ) -> tuple[Task, ...]:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresTaskPanelWriter(session).list_inbox(
                    access_context, limit
                )

    async def complete(self, command: CompleteTaskCommand) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresTaskPanelWriter(session).complete(command)


class PostgresTaskPanelWriter:
    """Lists and completes tasks through a transaction owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_inbox(
        self, access_context: AccessContext, limit: int
    ) -> tuple[Task, ...]:
        await _set_user_space_scope(self._session, access_context)
        models = await self._session.scalars(
            select(TaskModel)
            .where(
                TaskModel.user_space_id == access_context.user_space_id,
                TaskModel.status == TaskStatus.INBOX,
            )
            .order_by(TaskModel.created_at, TaskModel.id)
            .limit(limit)
        )
        return tuple(_to_entity(model) for model in models)

    async def complete(self, command: CompleteTaskCommand) -> bool:
        await _set_user_space_scope(self._session, command.access_context)
        completed_task_id = await self._session.scalar(
            update(TaskModel)
            .where(
                TaskModel.id == command.task_id,
                TaskModel.user_space_id == command.access_context.user_space_id,
                TaskModel.status == TaskStatus.INBOX,
            )
            .values(
                status=TaskStatus.COMPLETED,
                updated_at=command.completed_at,
            )
            .returning(TaskModel.id)
        )
        return completed_task_id is not None


class PostgresPendingCaptureSelectionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresPendingCaptureSelectionWriter(session).set_awaiting_task(
                    command
                )

    async def set_selection(self, command: SetPendingCaptureSelectionCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresPendingCaptureSelectionWriter(session).set_selection(
                    command
                )

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresPendingCaptureSelectionWriter(session).cancel(command)

    async def consume_selection(
        self, command: ConsumePendingCaptureSelectionCommand
    ) -> PendingCaptureType | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresPendingCaptureSelectionWriter(
                    session
                ).consume_selection(command)

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresPendingCaptureSelectionWriter(
                    session
                ).consume_awaiting_task(command)


class PostgresPendingCaptureSelectionWriter:
    """Mutates pending state and creates Tasks through a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_awaiting_task(self, command: SetAwaitingTaskCommand) -> None:
        await self.set_selection(
            SetPendingCaptureSelectionCommand(
                access_context=command.access_context,
                selection=PendingCaptureType.TASK.value,
                updated_at=command.updated_at,
                trace_id=command.trace_id,
            )
        )

    async def set_selection(self, command: SetPendingCaptureSelectionCommand) -> None:
        selection = await self._get_or_create(
            command.access_context, command.updated_at, command.trace_id
        )
        selection.selection = PendingCaptureType(command.selection)
        selection.updated_at = command.updated_at
        selection.trace_id = command.trace_id
        await self._session.flush()

    async def cancel(self, command: CancelPendingTaskCommand) -> None:
        # Отмена = очистить явный выбор. Строка живёт ТОЛЬКО пока ждёт явный
        # выбор; её отсутствие означает «кнопку не нажимали» (дефолт).
        await _set_user_space_scope(self._session, command.access_context)
        selection = await self._session.scalar(
            select(PendingCaptureSelectionModel)
            .where(
                PendingCaptureSelectionModel.user_space_id
                == command.access_context.user_space_id
            )
            .with_for_update()
        )
        if selection is not None:
            await self._session.delete(selection)
            await self._session.flush()

    async def consume_selection(
        self, command: ConsumePendingCaptureSelectionCommand
    ) -> PendingCaptureType | None:
        await _set_user_space_scope(self._session, command.access_context)
        selection = await self._session.scalar(
            select(PendingCaptureSelectionModel)
            .where(
                PendingCaptureSelectionModel.user_space_id
                == command.access_context.user_space_id
            )
            .with_for_update()
        )
        # Нет строки → кнопку не нажимали: явного выбора нет (дефолт). Явно
        # выбранный тип ПОТРЕБЛЯЕМ, удаляя строку (не сбрасываем в NOTE — иначе
        # «нажал Заметку» не отличить от «не нажал ничего»).
        if selection is None:
            return None
        selected_type = selection.selection
        await self._session.delete(selection)
        await self._session.flush()
        return selected_type

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        await _set_user_space_scope(self._session, command.access_context)
        selection = await self._session.scalar(
            select(PendingCaptureSelectionModel)
            .where(
                PendingCaptureSelectionModel.user_space_id
                == command.access_context.user_space_id
            )
            .with_for_update()
        )
        if selection is None or selection.selection is not PendingCaptureType.TASK:
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
        await self._session.delete(selection)
        await self._session.flush()
        return task

    async def _get_or_create(
        self, access_context: AccessContext, updated_at: datetime, trace_id: str
    ) -> PendingCaptureSelectionModel:
        await _set_user_space_scope(self._session, access_context)
        selection = await self._session.scalar(
            select(PendingCaptureSelectionModel)
            .where(
                PendingCaptureSelectionModel.user_space_id
                == access_context.user_space_id
            )
            .with_for_update()
        )
        if selection is not None:
            return selection

        selection = PendingCaptureSelectionModel(
            user_space_id=access_context.user_space_id,
            selection=PendingCaptureType.NOTE,
            updated_at=updated_at,
            trace_id=trace_id,
        )
        self._session.add(selection)
        await self._session.flush()
        return selection


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

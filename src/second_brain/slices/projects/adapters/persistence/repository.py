from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Table, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    IdeaModel,
    NoteModel,
    QuestionModel,
)
from second_brain.slices.projects.adapters.persistence.models import (
    ProjectCaptureEventLinkModel,
    ProjectContextModel,
    ProjectDecisionLinkModel,
    ProjectIdeaLinkModel,
    ProjectModel,
    ProjectNoteLinkModel,
    ProjectQuestionLinkModel,
    ProjectTaskLinkModel,
)
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    CancelProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    InheritCaptureProjectLinksCommand,
    LinkCurrentProjectToCaptureCommand,
    LinkProjectContentCommand,
    SelectProjectCommand,
)
from second_brain.slices.projects.domain.entities import Project, ProjectContentKind
from second_brain.slices.tasks.adapters.persistence.models import TaskModel


@dataclass(frozen=True)
class _LinkDefinition:
    link_table: Table
    target_table: Table
    target_id_column: str


_LINKS = {
    ProjectContentKind.CAPTURE_EVENT: _LinkDefinition(
        cast(Table, ProjectCaptureEventLinkModel.__table__),
        cast(Table, CaptureEventModel.__table__),
        "capture_event_id",
    ),
    ProjectContentKind.NOTE: _LinkDefinition(
        cast(Table, ProjectNoteLinkModel.__table__),
        cast(Table, NoteModel.__table__),
        "note_id",
    ),
    ProjectContentKind.TASK: _LinkDefinition(
        cast(Table, ProjectTaskLinkModel.__table__),
        cast(Table, TaskModel.__table__),
        "task_id",
    ),
    ProjectContentKind.IDEA: _LinkDefinition(
        cast(Table, ProjectIdeaLinkModel.__table__),
        cast(Table, IdeaModel.__table__),
        "idea_id",
    ),
    ProjectContentKind.DECISION: _LinkDefinition(
        cast(Table, ProjectDecisionLinkModel.__table__),
        cast(Table, DecisionModel.__table__),
        "decision_id",
    ),
    ProjectContentKind.QUESTION: _LinkDefinition(
        cast(Table, ProjectQuestionLinkModel.__table__),
        cast(Table, QuestionModel.__table__),
        "question_id",
    ),
}


class PostgresProjectRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_awaiting_creation(self, command: BeginProjectCreationCommand) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresProjectWriter(session).set_awaiting_creation(command)

    async def cancel_awaiting_creation(
        self, command: CancelProjectCreationCommand
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresProjectWriter(session).cancel_awaiting_creation(command)

    async def lock_awaiting_creation(self, access_context: AccessContext) -> bool:
        async with self._session_factory() as session, session.begin():
            return await PostgresProjectWriter(session).lock_awaiting_creation(
                access_context
            )

    async def create_or_select(
        self,
        command: ConsumeProjectNameCommand,
        name: str,
        name_key: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresProjectWriter(session).create_or_select(
                command, name, name_key
            )

    async def list_projects(self, access_context: AccessContext) -> tuple[Project, ...]:
        async with self._session_factory() as session, session.begin():
            return await PostgresProjectWriter(session).list_projects(access_context)

    async def get_current_project_id(
        self, access_context: AccessContext
    ) -> UUID | None:
        async with self._session_factory() as session, session.begin():
            return await PostgresProjectWriter(session).get_current_project_id(
                access_context
            )

    async def select(self, command: SelectProjectCommand) -> bool:
        async with self._session_factory() as session, session.begin():
            return await PostgresProjectWriter(session).select(command)

    async def clear(self, command: ClearCurrentProjectCommand) -> bool:
        async with self._session_factory() as session, session.begin():
            return await PostgresProjectWriter(session).clear(command)


class PostgresProjectWriter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_awaiting_creation(self, command: BeginProjectCreationCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        statement = (
            postgresql_insert(ProjectContextModel)
            .values(
                user_space_id=command.access_context.user_space_id,
                current_project_id=None,
                awaiting_name=True,
                updated_at=command.updated_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_update(
                index_elements=[ProjectContextModel.user_space_id],
                set_={
                    "awaiting_name": True,
                    "updated_at": command.updated_at,
                    "trace_id": command.trace_id,
                },
            )
        )
        await self._session.execute(statement)

    async def cancel_awaiting_creation(
        self, command: CancelProjectCreationCommand
    ) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        await self._session.execute(
            update(ProjectContextModel)
            .where(
                ProjectContextModel.user_space_id
                == command.access_context.user_space_id
            )
            .values(
                awaiting_name=False,
                updated_at=command.updated_at,
                trace_id=command.trace_id,
            )
        )

    async def lock_awaiting_creation(self, access_context: AccessContext) -> bool:
        await _set_user_space_scope(self._session, access_context)
        awaiting = await self._session.scalar(
            select(ProjectContextModel.awaiting_name)
            .where(ProjectContextModel.user_space_id == access_context.user_space_id)
            .with_for_update()
        )
        return awaiting is True

    async def create_or_select(
        self,
        command: ConsumeProjectNameCommand,
        name: str,
        name_key: str,
    ) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        await self._session.execute(
            postgresql_insert(ProjectModel)
            .values(
                id=uuid4(),
                user_space_id=command.access_context.user_space_id,
                name=name,
                name_key=name_key,
                created_at=command.created_at,
                updated_at=command.created_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_nothing(
                index_elements=[ProjectModel.user_space_id, ProjectModel.name_key]
            )
        )
        project_id = await self._session.scalar(
            select(ProjectModel.id).where(
                ProjectModel.user_space_id == command.access_context.user_space_id,
                ProjectModel.name_key == name_key,
            )
        )
        if project_id is None:
            raise RuntimeError("project create did not produce a scoped project")
        await self._session.execute(
            update(ProjectContextModel)
            .where(
                ProjectContextModel.user_space_id
                == command.access_context.user_space_id
            )
            .values(
                current_project_id=project_id,
                awaiting_name=False,
                updated_at=command.created_at,
                trace_id=command.trace_id,
            )
        )

    async def list_projects(self, access_context: AccessContext) -> tuple[Project, ...]:
        await _set_user_space_scope(self._session, access_context)
        models = await self._session.scalars(
            select(ProjectModel)
            .where(ProjectModel.user_space_id == access_context.user_space_id)
            .order_by(ProjectModel.created_at, ProjectModel.id)
        )
        return tuple(_to_project(model) for model in models)

    async def get_current_project_id(
        self, access_context: AccessContext
    ) -> UUID | None:
        await _set_user_space_scope(self._session, access_context)
        return await self._session.scalar(
            select(ProjectContextModel.current_project_id).where(
                ProjectContextModel.user_space_id == access_context.user_space_id
            )
        )

    async def select(self, command: SelectProjectCommand) -> bool:
        await _set_user_space_scope(self._session, command.access_context)
        project_id = await self._session.scalar(
            select(ProjectModel.id).where(
                ProjectModel.id == command.project_id,
                ProjectModel.user_space_id == command.access_context.user_space_id,
            )
        )
        if project_id is None:
            return False
        await self._upsert_context(
            command.access_context,
            project_id,
            awaiting_name=False,
            updated_at=command.updated_at,
            trace_id=command.trace_id,
        )
        return True

    async def clear(self, command: ClearCurrentProjectCommand) -> bool:
        await _set_user_space_scope(self._session, command.access_context)
        context = await self._session.scalar(
            select(ProjectContextModel)
            .where(
                ProjectContextModel.user_space_id
                == command.access_context.user_space_id
            )
            .with_for_update()
        )
        if context is None:
            return False
        changed = context.current_project_id is not None
        context.current_project_id = None
        context.awaiting_name = False
        context.updated_at = command.updated_at
        context.trace_id = command.trace_id
        await self._session.flush()
        return changed

    async def _upsert_context(
        self,
        access_context: AccessContext,
        project_id: UUID | None,
        *,
        awaiting_name: bool,
        updated_at: datetime,
        trace_id: str,
    ) -> None:
        statement = (
            postgresql_insert(ProjectContextModel)
            .values(
                user_space_id=access_context.user_space_id,
                current_project_id=project_id,
                awaiting_name=awaiting_name,
                updated_at=updated_at,
                trace_id=trace_id,
            )
            .on_conflict_do_update(
                index_elements=[ProjectContextModel.user_space_id],
                set_={
                    "current_project_id": project_id,
                    "awaiting_name": awaiting_name,
                    "updated_at": updated_at,
                    "trace_id": trace_id,
                },
            )
        )
        await self._session.execute(statement)


class PostgresProjectContentLinkRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def link(self, command: LinkProjectContentCommand) -> bool:
        async with self._session_factory() as session, session.begin():
            return await PostgresProjectContentLinkWriter(session).link(command)

    async def link_current_to_capture(
        self, command: LinkCurrentProjectToCaptureCommand
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresProjectContentLinkWriter(session).link_current_to_capture(
                command
            )

    async def inherit_capture_links(
        self, command: InheritCaptureProjectLinksCommand
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresProjectContentLinkWriter(session).inherit_capture_links(
                command
            )


class PostgresProjectContentLinkWriter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def link(self, command: LinkProjectContentCommand) -> bool:
        await _set_user_space_scope(self._session, command.access_context)
        project_exists = await self._session.scalar(
            select(ProjectModel.id).where(
                ProjectModel.id == command.project_id,
                ProjectModel.user_space_id == command.access_context.user_space_id,
            )
        )
        if project_exists is None:
            return False
        definition = _LINKS[command.content_kind]
        target_exists = await self._session.scalar(
            select(definition.target_table.c.id).where(
                definition.target_table.c.id == command.content_id,
                definition.target_table.c.user_space_id
                == command.access_context.user_space_id,
            )
        )
        if target_exists is None:
            return False
        values = {
            "project_id": command.project_id,
            definition.target_id_column: command.content_id,
            "user_space_id": command.access_context.user_space_id,
            "created_at": command.created_at,
            "trace_id": command.trace_id,
        }
        inserted = await self._session.scalar(
            postgresql_insert(definition.link_table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(definition.link_table.c.project_id)
        )
        return inserted is not None

    async def link_current_to_capture(
        self, command: LinkCurrentProjectToCaptureCommand
    ) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        project_id = await self._session.scalar(
            select(ProjectContextModel.current_project_id).where(
                ProjectContextModel.user_space_id
                == command.access_context.user_space_id
            )
        )
        if project_id is None:
            return
        await self.link(
            LinkProjectContentCommand(
                access_context=command.access_context,
                project_id=project_id,
                content_kind=ProjectContentKind.CAPTURE_EVENT,
                content_id=command.capture_event_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )

    async def inherit_capture_links(
        self, command: InheritCaptureProjectLinksCommand
    ) -> None:
        if command.content_kind is ProjectContentKind.CAPTURE_EVENT:
            raise ValueError("a capture event cannot inherit from itself")
        await _set_user_space_scope(self._session, command.access_context)
        project_ids = await self._session.scalars(
            select(ProjectCaptureEventLinkModel.project_id).where(
                ProjectCaptureEventLinkModel.capture_event_id
                == command.source_capture_event_id,
                ProjectCaptureEventLinkModel.user_space_id
                == command.access_context.user_space_id,
            )
        )
        for project_id in project_ids:
            await self.link(
                LinkProjectContentCommand(
                    access_context=command.access_context,
                    project_id=project_id,
                    content_kind=command.content_kind,
                    content_id=command.content_id,
                    created_at=command.created_at,
                    trace_id=command.trace_id,
                )
            )


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_project(model: ProjectModel) -> Project:
    return Project(
        id=model.id,
        user_space_id=model.user_space_id,
        name=model.name,
        created_at=model.created_at,
        updated_at=model.updated_at,
        trace_id=model.trace_id,
    )

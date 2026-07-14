from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.repository import (
    PostgresNoteRepository,
)
from second_brain.slices.knowledge.application.contracts import CreateNoteCommand
from second_brain.slices.projects.adapters.persistence.models import (
    ProjectNoteLinkModel,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkRepository,
    PostgresProjectRepository,
)
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    LinkProjectContentCommand,
    SelectProjectCommand,
)
from second_brain.slices.projects.application.projects import Projects
from second_brain.slices.projects.domain.entities import ProjectContentKind
from tests.projects.conftest import ACCESS_A, ACCESS_B, NOW

TRACE_ID = "1" * 32


def capture_command(access: AccessContext, update_id: int) -> CaptureTextCommand:
    return CaptureTextCommand(
        access_context=access,
        bot_id=100,
        telegram_update_id=update_id,
        telegram_message_id=update_id + 1000,
        raw_text=f"source {update_id}",
        received_at=NOW,
        trace_id=TRACE_ID,
    )


async def create_project(projects: Projects, access: AccessContext, name: str) -> UUID:
    await projects.begin_creation(BeginProjectCreationCommand(access, NOW, TRACE_ID))
    result = await projects.consume_name(
        ConsumeProjectNameCommand(access, name, NOW, TRACE_ID)
    )
    assert result is not None
    assert result.current_project_id is not None
    return result.current_project_id


@pytest.mark.asyncio
async def test_project_name_scope_current_selection_and_clear_are_persistent(
    engine: AsyncEngine,
) -> None:
    repository = PostgresProjectRepository(create_session_factory(engine))
    projects = Projects(repository)

    project_a = await create_project(projects, ACCESS_A, "  Second Brain  ")
    duplicate_a = await create_project(projects, ACCESS_A, "second brain")
    project_b = await create_project(projects, ACCESS_B, "Second Brain")

    listed_a = await projects.list_projects(ACCESS_A)
    listed_b = await projects.list_projects(ACCESS_B)
    assert [item.name for item in listed_a.items] == ["Second Brain"]
    assert listed_a.current_project_id == project_a == duplicate_a
    assert listed_b.current_project_id == project_b
    assert project_b != project_a

    rejected = await projects.select(
        SelectProjectCommand(ACCESS_A, project_b, NOW, TRACE_ID)
    )
    assert rejected.action_succeeded is False
    assert rejected.current_project_id == project_a

    cleared = await projects.clear(ClearCurrentProjectCommand(ACCESS_A, NOW, TRACE_ID))
    assert cleared.action_succeeded is True
    assert (await projects.list_projects(ACCESS_A)).current_project_id is None
    assert (await projects.list_projects(ACCESS_B)).current_project_id == project_b


@pytest.mark.asyncio
async def test_record_can_link_to_two_projects_and_duplicate_is_a_noop(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    links = PostgresProjectContentLinkRepository(factory)
    capture = await PostgresCaptureEventRepository(factory).create(
        capture_command(ACCESS_A, 1)
    )
    note = await PostgresNoteRepository(factory).create(
        CreateNoteCommand(ACCESS_A, "note", capture.id, NOW, TRACE_ID)
    )
    first_project = await create_project(projects, ACCESS_A, "First")
    second_project = await create_project(projects, ACCESS_A, "Second")

    first = await links.link(
        LinkProjectContentCommand(
            ACCESS_A,
            first_project,
            ProjectContentKind.NOTE,
            note.id,
            NOW,
            TRACE_ID,
        )
    )
    duplicate = await links.link(
        LinkProjectContentCommand(
            ACCESS_A,
            first_project,
            ProjectContentKind.NOTE,
            note.id,
            NOW,
            TRACE_ID,
        )
    )
    second = await links.link(
        LinkProjectContentCommand(
            ACCESS_A,
            second_project,
            ProjectContentKind.NOTE,
            note.id,
            NOW,
            TRACE_ID,
        )
    )

    assert (first, duplicate, second) == (True, False, True)
    async with create_session_factory(schema_engine)() as owner_session:
        count = await owner_session.scalar(
            select(func.count()).select_from(ProjectNoteLinkModel)
        )
    assert count == 2


@pytest.mark.asyncio
async def test_link_rejects_foreign_project_and_foreign_content_without_leaking(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    links = PostgresProjectContentLinkRepository(factory)
    project_a = await create_project(projects, ACCESS_A, "A")
    project_b = await create_project(projects, ACCESS_B, "B")
    source_b = await PostgresCaptureEventRepository(factory).create(
        capture_command(ACCESS_B, 2)
    )
    note_b = await PostgresNoteRepository(factory).create(
        CreateNoteCommand(ACCESS_B, "private", source_b.id, NOW, TRACE_ID)
    )

    foreign_project = await links.link(
        LinkProjectContentCommand(
            ACCESS_A,
            project_b,
            ProjectContentKind.NOTE,
            note_b.id,
            NOW,
            TRACE_ID,
        )
    )
    foreign_content = await links.link(
        LinkProjectContentCommand(
            ACCESS_A,
            project_a,
            ProjectContentKind.NOTE,
            note_b.id,
            NOW,
            TRACE_ID,
        )
    )

    assert foreign_project is False
    assert foreign_content is False

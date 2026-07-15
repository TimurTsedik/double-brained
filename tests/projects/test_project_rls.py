import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.knowledge.adapters.persistence.repository import (
    PostgresNoteRepository,
)
from second_brain.slices.knowledge.application.contracts import CreateNoteCommand
from second_brain.slices.projects.adapters.persistence.models import (
    ProjectContextModel,
    ProjectModel,
    ProjectNoteLinkModel,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkRepository,
    PostgresProjectRepository,
)
from second_brain.slices.projects.application.contracts import (
    LinkProjectContentCommand,
)
from second_brain.slices.projects.application.projects import Projects
from second_brain.slices.projects.domain.entities import ProjectContentKind
from tests.identity.conftest import IsolatedDatabase
from tests.projects.conftest import ACCESS_A, ACCESS_B, NOW
from tests.projects.test_project_persistence import (
    TRACE_ID,
    capture_command,
    create_project,
)

PROJECT_TABLES = (
    "projects",
    "project_contexts",
    "project_capture_event_links",
    "project_note_links",
    "project_task_links",
    "project_idea_links",
    "project_decision_links",
    "project_question_links",
)


@pytest.mark.asyncio
async def test_forced_rls_hides_other_space_projects_context_and_links(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    project_a = await create_project(projects, ACCESS_A, "A")
    project_b = await create_project(projects, ACCESS_B, "B")
    links = PostgresProjectContentLinkRepository(factory)
    for update_id, access, project_id in (
        (1, ACCESS_A, project_a),
        (2, ACCESS_B, project_b),
    ):
        source = await PostgresCaptureEventRepository(factory).create(
            capture_command(access, update_id)
        )
        note = await PostgresNoteRepository(factory).create(
            CreateNoteCommand(access, "private", source.id, NOW, TRACE_ID)
        )
        assert await links.link(
            LinkProjectContentCommand(
                access,
                project_id,
                ProjectContentKind.NOTE,
                note.id,
                NOW,
                TRACE_ID,
            )
        )

    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :value, true)"),
        {"value": str(ACCESS_A.user_space_id)},
    )

    assert (await session.scalars(select(ProjectModel.id))).all() == [project_a]
    assert await session.scalar(select(func.count()).select_from(ProjectModel)) == 1
    assert (
        await session.scalar(select(func.count()).select_from(ProjectContextModel)) == 1
    )
    assert (await session.scalars(select(ProjectNoteLinkModel.project_id))).all() == [
        project_a
    ]


@pytest.mark.asyncio
async def test_forced_rls_hides_admin_projects_from_the_member_side(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    # Реципрокно: под scope member'а (B) виден только его проект — проект admin'а
    # (A) не читается. Приватность в обе стороны, admin НЕ суперпользователь.
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    project_a = await create_project(projects, ACCESS_A, "A")
    project_b = await create_project(projects, ACCESS_B, "B")
    links = PostgresProjectContentLinkRepository(factory)
    for update_id, access, project_id in (
        (11, ACCESS_A, project_a),
        (12, ACCESS_B, project_b),
    ):
        source = await PostgresCaptureEventRepository(factory).create(
            capture_command(access, update_id)
        )
        note = await PostgresNoteRepository(factory).create(
            CreateNoteCommand(access, "private", source.id, NOW, TRACE_ID)
        )
        assert await links.link(
            LinkProjectContentCommand(
                access,
                project_id,
                ProjectContentKind.NOTE,
                note.id,
                NOW,
                TRACE_ID,
            )
        )

    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :value, true)"),
        {"value": str(ACCESS_B.user_space_id)},
    )

    assert (await session.scalars(select(ProjectModel.id))).all() == [project_b]
    assert await session.scalar(select(func.count()).select_from(ProjectModel)) == 1
    assert (
        await session.scalar(select(func.count()).select_from(ProjectContextModel)) == 1
    )
    assert (await session.scalars(select(ProjectNoteLinkModel.project_id))).all() == [
        project_b
    ]


@pytest.mark.asyncio
async def test_owner_insert_cannot_create_cross_space_typed_link(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    project_a = await create_project(projects, ACCESS_A, "A")
    source_b = await PostgresCaptureEventRepository(factory).create(
        capture_command(ACCESS_B, 10)
    )
    note_b = await PostgresNoteRepository(factory).create(
        CreateNoteCommand(ACCESS_B, "private", source_b.id, NOW, TRACE_ID)
    )

    async with create_session_factory(schema_engine)() as owner_session:
        with pytest.raises(IntegrityError):
            await owner_session.execute(
                insert(ProjectNoteLinkModel).values(
                    project_id=project_a,
                    note_id=note_b.id,
                    user_space_id=ACCESS_A.user_space_id,
                    created_at=NOW,
                    trace_id=TRACE_ID,
                )
            )


@pytest.mark.asyncio
async def test_all_project_tables_force_rls_and_typed_links_have_two_owner_fks(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    async with schema_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relname = ANY(:tables)"
                ),
                {"schema": schema, "tables": list(PROJECT_TABLES)},
            )
        ).all()
        constraint_count = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :schema "
                "AND t.relname LIKE 'project_%_links' AND c.contype = 'f'"
            ),
            {"schema": schema},
        )

    assert {row[0]: (row[1], row[2]) for row in rows} == {
        table: (True, True) for table in PROJECT_TABLES
    }
    assert constraint_count == 18


@pytest.mark.asyncio
async def test_application_role_has_least_project_privileges(
    session: AsyncSession,
) -> None:
    expected = {
        "projects": {"SELECT", "INSERT"},
        "project_contexts": {"SELECT", "INSERT", "UPDATE"},
        **{
            table: {"SELECT", "INSERT"}
            for table in PROJECT_TABLES
            if table not in {"projects", "project_contexts"}
        },
    }
    for table_name, allowed in expected.items():
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            actual = await session.scalar(
                text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                {"table": table_name, "privilege": privilege},
            )
            assert actual is (privilege in allowed), (table_name, privilege)

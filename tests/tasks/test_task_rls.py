from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
    TaskProvenanceModel,
)
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresPendingCaptureSelectionRepository,
    PostgresTaskPanelRepository,
    PostgresTaskRepository,
)
from second_brain.slices.tasks.application.contracts import (
    CompleteTaskCommand,
    CreateTaskCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType, TaskStatus
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_task_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                {
                    "id": ACCESS_A.user_id,
                    # Пространство A = admin, B = member: доказывает, что admin
                    # НЕ суперпользователь — RLS изолирует по user_space_id.
                    "role": "admin",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": ACCESS_A.user_space_id,
                    "owner_user_id": ACCESS_A.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_space_id,
                    "owner_user_id": ACCESS_B.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )


@pytest_asyncio.fixture
async def capture_repository(engine: AsyncEngine) -> PostgresCaptureEventRepository:
    return PostgresCaptureEventRepository(create_session_factory(engine))


@pytest_asyncio.fixture
async def task_repository(engine: AsyncEngine) -> PostgresTaskRepository:
    return PostgresTaskRepository(create_session_factory(engine))


@pytest_asyncio.fixture
async def task_panel_store(engine: AsyncEngine) -> PostgresTaskPanelRepository:
    return PostgresTaskPanelRepository(create_session_factory(engine))


def capture_command(
    access_context: AccessContext, *, update_id: int
) -> CaptureTextCommand:
    return CaptureTextCommand(
        access_context=access_context,
        bot_id=100,
        telegram_update_id=update_id,
        telegram_message_id=update_id + 1000,
        raw_text=f"source {update_id}",
        received_at=NOW,
        trace_id="1" * 32,
    )


def task_command(
    access_context: AccessContext,
    *,
    source_capture_event_id: UUID,
    title: str = "  task title  ",
    created_at: datetime = NOW,
) -> CreateTaskCommand:
    return CreateTaskCommand(
        access_context=access_context,
        title=title,
        source_capture_event_id=source_capture_event_id,
        created_at=created_at,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
async def test_task_panel_lists_oldest_open_tasks_and_completes_own_task(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    task_panel_store: PostgresTaskPanelRepository,
    schema_engine: AsyncEngine,
) -> None:
    old_source = await capture_repository.create(
        capture_command(ACCESS_A, update_id=180)
    )
    new_source = await capture_repository.create(
        capture_command(ACCESS_A, update_id=181)
    )
    old_task = await task_repository.create(
        task_command(
            ACCESS_A,
            source_capture_event_id=old_source.id,
            title="old task",
            created_at=NOW,
        )
    )
    new_task = await task_repository.create(
        task_command(
            ACCESS_A,
            source_capture_event_id=new_source.id,
            title="new task",
            created_at=NOW + timedelta(minutes=1),
        )
    )

    listed = await task_panel_store.list_inbox(ACCESS_A, 10)
    changed = await task_panel_store.complete(
        CompleteTaskCommand(
            access_context=ACCESS_A,
            task_id=old_task.id,
            completed_at=NOW + timedelta(hours=1),
            trace_id="2" * 32,
        )
    )
    refreshed = await task_panel_store.list_inbox(ACCESS_A, 10)

    assert [task.id for task in listed] == [old_task.id, new_task.id]
    assert changed is True
    assert [task.id for task in refreshed] == [new_task.id]
    async with create_session_factory(schema_engine)() as session:
        completed = await session.get(TaskModel, old_task.id)
    assert completed is not None
    assert completed.status is TaskStatus.COMPLETED
    assert completed.updated_at == NOW + timedelta(hours=1)
    assert completed.trace_id == "1" * 32


@pytest.mark.asyncio
async def test_task_panel_cannot_list_or_complete_another_space_task(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    task_panel_store: PostgresTaskPanelRepository,
    schema_engine: AsyncEngine,
) -> None:
    source_b = await capture_repository.create(capture_command(ACCESS_B, update_id=190))
    task_b = await task_repository.create(
        task_command(ACCESS_B, source_capture_event_id=source_b.id, title="secret b")
    )

    listed = await task_panel_store.list_inbox(ACCESS_A, 10)
    changed = await task_panel_store.complete(
        CompleteTaskCommand(
            access_context=ACCESS_A,
            task_id=task_b.id,
            completed_at=NOW + timedelta(hours=1),
            trace_id="2" * 32,
        )
    )

    assert listed == ()
    assert changed is False
    async with create_session_factory(schema_engine)() as session:
        untouched = await session.get(TaskModel, task_b.id)
    assert untouched is not None
    assert untouched.status is TaskStatus.INBOX


@pytest.mark.asyncio
async def test_member_cannot_list_or_complete_admin_task(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    task_panel_store: PostgresTaskPanelRepository,
    schema_engine: AsyncEngine,
) -> None:
    # Реципрокно: member (B) не видит и не закрывает задачу admin'а (A) —
    # приватность в обе стороны, admin НЕ суперпользователь.
    source_a = await capture_repository.create(capture_command(ACCESS_A, update_id=191))
    task_a = await task_repository.create(
        task_command(ACCESS_A, source_capture_event_id=source_a.id, title="secret a")
    )

    listed = await task_panel_store.list_inbox(ACCESS_B, 10)
    changed = await task_panel_store.complete(
        CompleteTaskCommand(
            access_context=ACCESS_B,
            task_id=task_a.id,
            completed_at=NOW + timedelta(hours=1),
            trace_id="3" * 32,
        )
    )

    assert listed == ()
    assert changed is False
    async with create_session_factory(schema_engine)() as session:
        untouched = await session.get(TaskModel, task_a.id)
    assert untouched is not None
    assert untouched.status is TaskStatus.INBOX


@pytest.mark.asyncio
async def test_rls_hides_space_b_task_and_provenance_from_space_a(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    session: AsyncSession,
) -> None:
    source_a = await capture_repository.create(capture_command(ACCESS_A, update_id=200))
    source_b = await capture_repository.create(capture_command(ACCESS_B, update_id=201))
    task_a = await task_repository.create(
        task_command(ACCESS_A, source_capture_event_id=source_a.id)
    )
    await task_repository.create(
        task_command(ACCESS_B, source_capture_event_id=source_b.id)
    )

    await _set_scope(session, ACCESS_A)
    assert (
        await session.scalars(select(TaskModel.id).order_by(TaskModel.created_at))
    ).all() == [task_a.id]
    assert (await session.scalars(select(TaskProvenanceModel.task_id))).all() == [
        task_a.id
    ]
    assert await session.scalar(select(func.count()).select_from(TaskModel)) == 1
    provenance_count = await session.scalar(
        select(func.count()).select_from(TaskProvenanceModel)
    )
    assert provenance_count == 1


@pytest.mark.asyncio
async def test_space_a_cannot_insert_space_b_task_or_provenance(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    session: AsyncSession,
) -> None:
    source_b = await capture_repository.create(capture_command(ACCESS_B, update_id=210))
    task_b = await task_repository.create(
        task_command(ACCESS_B, source_capture_event_id=source_b.id)
    )
    another_source_b = await capture_repository.create(
        capture_command(ACCESS_B, update_id=211)
    )
    await _set_scope(session, ACCESS_A)

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(TaskModel).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                title="b",
                description=None,
                status=TaskStatus.INBOX.value,
                source_capture_event_id=source_b.id,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()

    await _set_scope(session, ACCESS_A)
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(TaskProvenanceModel).values(
                task_id=task_b.id,
                source_capture_event_id=another_source_b.id,
                user_space_id=ACCESS_B.user_space_id,
                created_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
async def test_app_role_cannot_read_or_insert_task_data_without_a_scope(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    session: AsyncSession,
) -> None:
    source_a = await capture_repository.create(capture_command(ACCESS_A, update_id=215))
    other_source_a = await capture_repository.create(
        capture_command(ACCESS_A, update_id=216)
    )
    task_a = await task_repository.create(
        task_command(ACCESS_A, source_capture_event_id=source_a.id)
    )

    assert (
        await session.scalar(select(TaskModel.id).where(TaskModel.id == task_a.id))
        is None
    )
    assert await session.scalar(select(func.count()).select_from(TaskModel)) == 0
    assert (
        await session.scalar(
            select(TaskProvenanceModel.task_id).where(
                TaskProvenanceModel.task_id == task_a.id
            )
        )
        is None
    )
    provenance_count = await session.scalar(
        select(func.count()).select_from(TaskProvenanceModel)
    )
    assert provenance_count == 0

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(TaskModel).values(
                id=uuid4(),
                user_space_id=ACCESS_A.user_space_id,
                title="a",
                description=None,
                status=TaskStatus.INBOX.value,
                source_capture_event_id=source_a.id,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(TaskProvenanceModel).values(
                task_id=task_a.id,
                source_capture_event_id=other_source_a.id,
                user_space_id=ACCESS_A.user_space_id,
                created_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
async def test_task_and_provenance_require_source_from_the_same_space(
    capture_repository: PostgresCaptureEventRepository,
    task_repository: PostgresTaskRepository,
    session: AsyncSession,
) -> None:
    source_a = await capture_repository.create(capture_command(ACCESS_A, update_id=220))
    source_b = await capture_repository.create(capture_command(ACCESS_B, update_id=221))
    task_b = await task_repository.create(
        task_command(ACCESS_B, source_capture_event_id=source_b.id)
    )
    task_id = uuid4()
    await _set_scope(session, ACCESS_B)

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(TaskModel).values(
                id=task_id,
                user_space_id=ACCESS_B.user_space_id,
                title="b",
                description=None,
                status=TaskStatus.INBOX.value,
                source_capture_event_id=source_a.id,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()

    await _set_scope(session, ACCESS_B)
    with pytest.raises(IntegrityError):
        await session.execute(
            insert(TaskProvenanceModel).values(
                task_id=task_b.id,
                source_capture_event_id=source_a.id,
                user_space_id=ACCESS_B.user_space_id,
                created_at=NOW,
                trace_id="1" * 32,
            )
        )

    assert source_b.user_space_id == ACCESS_B.user_space_id


@pytest.mark.asyncio
async def test_task_tables_have_forced_row_level_security(
    isolated_database: IsolatedDatabase,
    schema_engine: AsyncEngine,
) -> None:
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class "
                "WHERE oid = ANY(ARRAY["
                "to_regclass(:tasks), to_regclass(:provenance), to_regclass(:pending)"
                "]) "
                "ORDER BY relname"
            ),
            {
                "tasks": f"{isolated_database.schema}.tasks",
                "provenance": f"{isolated_database.schema}.task_provenance",
                "pending": f"{isolated_database.schema}.pending_capture_selections",
            },
        )

    assert result.all() == [
        ("pending_capture_selections", True, True),
        ("task_provenance", True, True),
        ("tasks", True, True),
    ]


@pytest.mark.asyncio
async def test_app_role_can_update_only_tasks_and_cannot_delete_task_tables(
    session: AsyncSession,
) -> None:
    # Грант на tasks КОЛОНОЧНЫЙ (title/status/updated_at/edited_at, S3) —
    # табличного UPDATE нет, проверяем has_ANY_column_privilege. Точный список
    # колонок закреплён в tests/identity/test_database_roles.py.
    task_update_allowed = await session.scalar(
        text("SELECT has_any_column_privilege(current_user, 'tasks', 'UPDATE')")
    )
    provenance_update_allowed = await session.scalar(
        text("SELECT has_table_privilege(current_user, 'task_provenance', 'UPDATE')")
    )

    assert task_update_allowed is True
    assert provenance_update_allowed is False

    for table_name in ("tasks", "task_provenance"):
        delete_allowed = await session.scalar(
            text("SELECT has_table_privilege(current_user, :table_name, 'DELETE')"),
            {"table_name": table_name},
        )

        assert delete_allowed is False


@pytest.mark.asyncio
async def test_pending_capture_selection_cannot_be_updated_without_its_user_space_scope(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    repository = PostgresPendingCaptureSelectionRepository(
        create_session_factory(engine)
    )
    await repository.set_awaiting_task(
        SetAwaitingTaskCommand(
            access_context=ACCESS_B,
            updated_at=NOW,
            trace_id="1" * 32,
        )
    )

    no_scope = await session.execute(
        update(PendingCaptureSelectionModel)
        .where(PendingCaptureSelectionModel.user_space_id == ACCESS_B.user_space_id)
        .values(selection=PendingCaptureType.NOTE)
    )
    assert no_scope.rowcount == 0
    await session.rollback()

    await _set_scope(session, ACCESS_A)
    foreign_scope = await session.execute(
        update(PendingCaptureSelectionModel)
        .where(PendingCaptureSelectionModel.user_space_id == ACCESS_B.user_space_id)
        .values(selection=PendingCaptureType.NOTE)
    )
    assert foreign_scope.rowcount == 0
    await session.rollback()

    async with create_session_factory(schema_engine)() as owner_session:
        selection = await owner_session.scalar(select(PendingCaptureSelectionModel))
    assert selection is not None
    assert selection.selection is PendingCaptureType.TASK


async def _set_scope(session: AsyncSession, access_context: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

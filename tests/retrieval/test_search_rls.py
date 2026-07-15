from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    IdeaModel,
    NoteModel,
    QuestionModel,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresExactSearchWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    MatchQuality,
    SearchRecordType,
)
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.tasks.domain.entities import TaskStatus
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_search_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                _user(ACCESS_A),
                _user(ACCESS_B),
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                _space(ACCESS_A),
                _space(ACCESS_B),
            ],
        )


def _user(access: AccessContext) -> dict[str, object]:
    # Пространство A = admin, B = member: admin НЕ суперпользователь (RLS по
    # user_space_id) — search-state изолирован в обе стороны.
    return {
        "id": access.user_id,
        "role": "admin" if access == ACCESS_A else "member",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _space(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_space_id,
        "owner_user_id": access.user_id,
        "timezone": "Asia/Jerusalem",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


async def _add_record(
    schema_engine: AsyncEngine,
    access: AccessContext,
    model: type[NoteModel | IdeaModel | DecisionModel | QuestionModel | TaskModel],
    content: str,
    *,
    created_at: datetime,
    task_status: TaskStatus = TaskStatus.INBOX,
) -> tuple[UUID, UUID]:
    source_id = uuid4()
    record_id = uuid4()
    update_id = int(source_id.int % 9_000_000_000) + 1
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=source_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 10_000_000_000,
                raw_text=content,
                received_at=created_at,
                created_at=created_at,
                trace_id="1" * 32,
            )
        )
        values: dict[str, object] = {
            "id": record_id,
            "user_space_id": access.user_space_id,
            "source_capture_event_id": source_id,
            "created_at": created_at,
            "updated_at": created_at,
            "trace_id": "1" * 32,
        }
        if model is TaskModel:
            values.update(
                title=content,
                description=None,
                status=task_status,
            )
        else:
            values["text"] = content
        await connection.execute(insert(model).values(**values))
    return record_id, source_id


async def _search(
    engine: AsyncEngine,
    access: AccessContext,
    query: str,
    limit: int = 10,
):
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await PostgresExactSearchWriter(session).search(access, query, limit)


@pytest.mark.asyncio
async def test_pending_mode_is_scoped_repeatable_and_cancellable(
    engine: AsyncEngine,
) -> None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            writer = PostgresExactSearchWriter(session)
            await writer.set_awaiting(SetAwaitingSearchCommand(ACCESS_A, NOW, "1" * 32))
            await writer.set_awaiting(
                SetAwaitingSearchCommand(ACCESS_A, NOW + timedelta(minutes=1), "2" * 32)
            )
            assert await writer.lock_pending(ACCESS_A) is True
            await writer.cancel(ACCESS_A)
            assert await writer.lock_pending(ACCESS_A) is False

    async with create_session_factory(engine)() as session:
        async with session.begin():
            writer = PostgresExactSearchWriter(session)
            await writer.set_awaiting(SetAwaitingSearchCommand(ACCESS_B, NOW, "3" * 32))
            assert await writer.lock_pending(ACCESS_A) is False


@pytest.mark.asyncio
async def test_search_returns_all_typed_records_with_provenance_and_task_state(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    inputs = [
        (NoteModel, "PostgreSQL note", TaskStatus.INBOX),
        (TaskModel, "PostgreSQL open task", TaskStatus.INBOX),
        (TaskModel, "PostgreSQL completed task", TaskStatus.COMPLETED),
        (IdeaModel, "PostgreSQL idea", TaskStatus.INBOX),
        (DecisionModel, "PostgreSQL decision", TaskStatus.INBOX),
        (QuestionModel, "PostgreSQL question", TaskStatus.INBOX),
    ]
    expected_sources: set[UUID] = set()
    for index, (model, content, task_status) in enumerate(inputs):
        _, source_id = await _add_record(
            schema_engine,
            ACCESS_A,
            model,
            content,
            created_at=NOW + timedelta(minutes=index),
            task_status=task_status,
        )
        expected_sources.add(source_id)

    results = await _search(engine, ACCESS_A, "postgres")

    assert [result.record_type for result in results] == [
        SearchRecordType.QUESTION,
        SearchRecordType.DECISION,
        SearchRecordType.IDEA,
        SearchRecordType.TASK,
        SearchRecordType.TASK,
        SearchRecordType.NOTE,
    ]
    assert {result.source_capture_event_id for result in results} == expected_sources
    assert [
        result.task_completed
        for result in results
        if result.record_type is SearchRecordType.TASK
    ] == [True, False]
    assert {result.match_quality for result in results} == {MatchQuality.SUBSTRING}


@pytest.mark.asyncio
async def test_substring_precedes_full_text_then_newest_breaks_ties(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_record(
        schema_engine,
        ACCESS_A,
        NoteModel,
        "alpha beta contiguous",
        created_at=NOW,
    )
    await _add_record(
        schema_engine,
        ACCESS_A,
        IdeaModel,
        "beta appears before alpha",
        created_at=NOW + timedelta(hours=1),
    )
    await _add_record(
        schema_engine,
        ACCESS_A,
        DecisionModel,
        "alpha beta newest",
        created_at=NOW + timedelta(hours=2),
    )

    results = await _search(engine, ACCESS_A, "alpha beta")

    assert [result.text for result in results] == [
        "alpha beta newest",
        "alpha beta contiguous",
        "beta appears before alpha",
    ]
    assert [result.match_quality for result in results] == [
        MatchQuality.SUBSTRING,
        MatchQuality.SUBSTRING,
        MatchQuality.FULL_TEXT,
    ]


@pytest.mark.asyncio
async def test_sql_wildcards_are_literal_and_results_are_limited(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_record(
        schema_engine,
        ACCESS_A,
        NoteModel,
        "budget is 100% complete",
        created_at=NOW,
    )
    await _add_record(
        schema_engine,
        ACCESS_A,
        NoteModel,
        "unrelated ordinary row",
        created_at=NOW + timedelta(minutes=1),
    )

    percent_results = await _search(engine, ACCESS_A, "100%")

    assert [result.text for result in percent_results] == ["budget is 100% complete"]

    for index in range(12):
        await _add_record(
            schema_engine,
            ACCESS_A,
            NoteModel,
            f"limited result {index}",
            created_at=NOW + timedelta(hours=1, minutes=index),
        )

    limited = await _search(engine, ACCESS_A, "limited", limit=10)

    assert len(limited) == 10
    assert limited[0].text == "limited result 11"
    assert limited[-1].text == "limited result 2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [NoteModel, TaskModel, IdeaModel, DecisionModel, QuestionModel],
)
async def test_search_cannot_reveal_another_space_record(
    model: type[NoteModel | IdeaModel | DecisionModel | QuestionModel | TaskModel],
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    await _add_record(
        schema_engine,
        ACCESS_B,
        model,
        "onlyb private match",
        created_at=NOW,
    )

    results = await _search(engine, ACCESS_A, "onlyb")

    assert results == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [NoteModel, TaskModel, IdeaModel, DecisionModel, QuestionModel],
)
async def test_member_search_cannot_reveal_admin_record(
    model: type[NoteModel | IdeaModel | DecisionModel | QuestionModel | TaskModel],
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    # Реципрокно: member (B) поиском не находит записи admin'а (A) — приватность
    # в обе стороны, admin НЕ суперпользователь.
    await _add_record(
        schema_engine,
        ACCESS_A,
        model,
        "onlya private match",
        created_at=NOW,
    )

    results = await _search(engine, ACCESS_B, "onlya")

    assert results == ()


@pytest.mark.asyncio
async def test_search_schema_has_expected_gin_indexes_and_app_privileges(
    session,
) -> None:
    indexes = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname LIKE 'ix_%_fts'"
        )
    )
    assert {name for name, _ in indexes} == {
        "ix_notes_text_fts",
        "ix_tasks_title_fts",
        "ix_ideas_text_fts",
        "ix_decisions_text_fts",
        "ix_questions_text_fts",
    }
    assert all("USING gin" in definition for _, definition in indexes)

    privileges = await session.execute(
        text(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = current_user "
            "AND table_schema = current_schema() "
            "AND table_name = 'pending_search_modes'"
        )
    )
    assert set(privileges.scalars()) == {"SELECT", "INSERT", "UPDATE", "DELETE"}

    assert await session.scalar(select(PendingSearchModeModel.user_space_id)) is None

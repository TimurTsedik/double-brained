from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.adapters.persistence.models import (
    MemoryAnswerModel,
    MemoryAnswerRunModel,
    MemoryAnswerSourceModel,
    MemoryAnswerStepModel,
    MemoryQuestionModel,
    MemoryRunEvidenceModel,
    PendingMemoryQuestionModel,
)
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryQueue,
)
from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    EvidenceSnippet,
    MemoryAnswer,
    MemoryRecordKind,
)
from second_brain.slices.memory.ports.repositories import (
    CreateMemoryQuestionCommand,
    SaveMemoryAnswerCommand,
    SnapshotEvidenceCommand,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_memory_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [_user(ACCESS_A), _user(ACCESS_B)])
        await connection.execute(
            insert(UserSpace), [_space(ACCESS_A), _space(ACCESS_B)]
        )


def _user(access: AccessContext) -> dict[str, object]:
    # Пространство A = admin, B = member: admin НЕ суперпользователь (RLS по
    # user_space_id) — изоляция ответов памяти держится в обе стороны.
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


async def _seed_full_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    access: AccessContext = ACCESS_B,
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(
        CreateMemoryQuestionCommand(
            access_context=access,
            bot_id=100,
            telegram_update_id=900,
            question_text="секрет B",
            current_project_id=None,
            created_at=NOW,
            trace_id="b" * 32,
        )
    )
    async with schema_engine.connect() as connection:
        run_id = await connection.scalar(
            select(MemoryAnswerRunModel.id).where(
                MemoryAnswerRunModel.question_id == question.id
            )
        )
    assert run_id is not None
    record_id = uuid4()
    capture_id = uuid4()
    await queue.snapshot_evidence(
        SnapshotEvidenceCommand(
            access,
            run_id,
            (
                EvidenceSnippet(
                    label="S1",
                    record_kind=MemoryRecordKind.NOTE,
                    record_id=record_id,
                    source_capture_event_id=capture_id,
                    created_at=NOW,
                    text="private B evidence",
                ),
            ),
        )
    )
    await queue.save_answer(
        SaveMemoryAnswerCommand(
            access_context=access,
            run_id=run_id,
            answer=MemoryAnswer(
                evidence_level=EvidenceLevel.DIRECT,
                answer_text="private B answer",
                sources=(
                    AnswerSource(
                        label="S1",
                        record_kind=MemoryRecordKind.NOTE,
                        record_id=record_id,
                        source_capture_event_id=capture_id,
                        created_at=NOW,
                    ),
                ),
                model_name="m",
                prompt_version="p",
                schema_version="s",
            ),
            created_at=NOW,
            trace_id="c" * 32,
        )
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(PendingMemoryQuestionModel).values(
                user_space_id=access.user_space_id,
                updated_at=NOW,
                trace_id="d" * 32,
            )
        )


@pytest.mark.asyncio
async def test_another_space_sees_none_of_b_rows(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _seed_full_run(engine, schema_engine)

    async with create_session_factory(engine)() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('second_brain.user_space_id', "
                    ":user_space_id, true)"
                ),
                {"user_space_id": str(ACCESS_A.user_space_id)},
            )
            for model in (
                MemoryQuestionModel,
                MemoryAnswerRunModel,
                MemoryAnswerStepModel,
                MemoryRunEvidenceModel,
                MemoryAnswerModel,
                MemoryAnswerSourceModel,
                PendingMemoryQuestionModel,
            ):
                count = await session.scalar(select(func.count()).select_from(model))
                assert count == 0, model.__name__


@pytest.mark.asyncio
async def test_member_space_sees_none_of_admin_rows(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Реципрокно: member (B) не читает ни одной строки памяти admin'а (A) —
    # приватность в ОБЕ стороны, admin НЕ суперпользователь.
    await _seed_full_run(engine, schema_engine, access=ACCESS_A)

    async with create_session_factory(engine)() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('second_brain.user_space_id', "
                    ":user_space_id, true)"
                ),
                {"user_space_id": str(ACCESS_B.user_space_id)},
            )
            for model in (
                MemoryQuestionModel,
                MemoryAnswerRunModel,
                MemoryAnswerStepModel,
                MemoryRunEvidenceModel,
                MemoryAnswerModel,
                MemoryAnswerSourceModel,
                PendingMemoryQuestionModel,
            ):
                count = await session.scalar(select(func.count()).select_from(model))
                assert count == 0, model.__name__


@pytest.mark.asyncio
async def test_repository_methods_do_not_read_across_spaces(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _seed_full_run(engine, schema_engine)
    async with schema_engine.connect() as connection:
        run_id = await connection.scalar(select(MemoryAnswerRunModel.id))
    assert run_id is not None

    queue = PostgresMemoryQueue(create_session_factory(engine))
    assert await queue.read_answer(ACCESS_A, run_id) is None
    assert await queue.read_run_question(ACCESS_A, run_id) is None
    assert await queue.read_reasoning_state(ACCESS_A, run_id) is None
    assert await queue.read_evidence_snapshot(ACCESS_A, run_id) == ()
    assert await queue.claim_due_run(ACCESS_A, NOW, LEASE) is None

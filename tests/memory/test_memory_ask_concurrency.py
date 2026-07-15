import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.memory_ask_in_transaction import MemoryAskInTransaction
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.adapters.persistence.models import (
    MemoryAnswerRunModel,
    MemoryAnswerStepModel,
    MemoryQuestionModel,
)
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryWriter,
)
from second_brain.slices.memory.application.contracts import (
    ConsumeMemoryQuestionCommand,
    MemoryAskResult,
    SetAwaitingMemoryCommand,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
BOT_ID = 100
TRACE = "a" * 32


@pytest_asyncio.fixture(autouse=True)
async def reset_memory_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="member",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=ACCESS.user_space_id,
                owner_user_id=ACCESS.user_id,
                timezone="Asia/Jerusalem",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


@pytest.mark.asyncio
async def test_memory_ask_concurrency_creates_exactly_one_question(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    session_factory = create_session_factory(engine)
    port = MemoryAskInTransaction()

    # Arm the one-shot mode in its own committed transaction.
    async with session_factory() as session, session.begin():
        await port.set_awaiting(
            SetAwaitingMemoryCommand(
                access_context=ACCESS, updated_at=NOW, trace_id=TRACE
            ),
            PostgresUpdateTransaction(session),
        )

    def _command(update_id: int) -> ConsumeMemoryQuestionCommand:
        return ConsumeMemoryQuestionCommand(
            access_context=ACCESS,
            bot_id=BOT_ID,
            telegram_update_id=update_id,
            question="что я решил про проект?",
            created_at=NOW,
            trace_id=TRACE,
        )

    # The winner takes FOR UPDATE on the mode row and then HOLDS its transaction
    # open until released, so the loser has no chance to slip past by ordering.
    winner_holds_lock = asyncio.Event()
    release_winner = asyncio.Event()

    async def winner() -> MemoryAskResult | None:
        async with session_factory() as session, session.begin():
            result = await port.consume_question(
                _command(1001), PostgresUpdateTransaction(session)
            )
            winner_holds_lock.set()
            await release_winner.wait()
            return result

    async def loser() -> MemoryAskResult | None:
        async with session_factory() as session, session.begin():
            return await port.consume_question(
                _command(1002), PostgresUpdateTransaction(session)
            )

    winner_task = asyncio.create_task(winner())
    await asyncio.wait_for(winner_holds_lock.wait(), timeout=5)

    # Only now start the loser; it must reach the FOR UPDATE and BLOCK on the
    # lock the winner still holds — it must not resolve while the winner is open.
    loser_task = asyncio.create_task(loser())
    await asyncio.sleep(0.3)
    assert not loser_task.done(), "loser did not block on the FOR UPDATE lock"

    # Release the winner: its commit deletes the mode row, and only now can the
    # blocked loser proceed — finding no mode and creating nothing.
    release_winner.set()
    first = await asyncio.wait_for(winner_task, timeout=5)
    second = await asyncio.wait_for(loser_task, timeout=5)

    assert first == MemoryAskResult(question_required=False)
    assert second is None

    async with schema_engine.connect() as connection:
        question_count = await connection.scalar(
            select(func.count()).select_from(MemoryQuestionModel)
        )
        run_count = await connection.scalar(
            select(func.count()).select_from(MemoryAnswerRunModel)
        )
        step_count = await connection.scalar(
            select(func.count()).select_from(MemoryAnswerStepModel)
        )
    assert question_count == 1
    assert run_count == 1
    assert step_count == 3


@pytest.mark.asyncio
async def test_consume_without_mode_returns_none(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    session_factory = create_session_factory(engine)
    port = MemoryAskInTransaction()

    async with session_factory() as session, session.begin():
        result = await port.consume_question(
            ConsumeMemoryQuestionCommand(
                access_context=ACCESS,
                bot_id=BOT_ID,
                telegram_update_id=2001,
                question="никакого режима нет",
                created_at=NOW,
                trace_id=TRACE,
            ),
            PostgresUpdateTransaction(session),
        )

    assert result is None
    async with schema_engine.connect() as connection:
        question_count = await connection.scalar(
            select(func.count()).select_from(MemoryQuestionModel)
        )
    assert question_count == 0


@pytest.mark.asyncio
async def test_blank_question_keeps_mode_and_creates_no_question(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    session_factory = create_session_factory(engine)
    port = MemoryAskInTransaction()

    async with session_factory() as session, session.begin():
        await PostgresMemoryWriter(session).set_awaiting(
            SetAwaitingMemoryCommand(
                access_context=ACCESS, updated_at=NOW, trace_id=TRACE
            )
        )

    async with session_factory() as session, session.begin():
        result = await port.consume_question(
            ConsumeMemoryQuestionCommand(
                access_context=ACCESS,
                bot_id=BOT_ID,
                telegram_update_id=2002,
                question="   \n  ",
                created_at=NOW,
                trace_id=TRACE,
            ),
            PostgresUpdateTransaction(session),
        )

    assert result == MemoryAskResult(question_required=True)
    # Mode survives: a follow-up real question still gets consumed.
    async with session_factory() as session, session.begin():
        again = await port.consume_question(
            ConsumeMemoryQuestionCommand(
                access_context=ACCESS,
                bot_id=BOT_ID,
                telegram_update_id=2003,
                question="теперь настоящий вопрос",
                created_at=NOW,
                trace_id=TRACE,
            ),
            PostgresUpdateTransaction(session),
        )
    assert again == MemoryAskResult(question_required=False)
    async with schema_engine.connect() as connection:
        question_count = await connection.scalar(
            select(func.count()).select_from(MemoryQuestionModel)
        )
    assert question_count == 1

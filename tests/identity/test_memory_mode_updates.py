from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.exact_search_in_transaction import ExactSearchInTransaction
from second_brain.bootstrap.project_context_in_transaction import (
    ProjectContextInTransaction,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    CaptureTextPort,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.memory.application.contracts import (
    ConsumeMemoryQuestionCommand,
    MemoryAskResult,
    SetAwaitingMemoryCommand,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")
TELEGRAM_USER_ID = 42


class FixedClock:
    def now(self) -> datetime:
        return NOW


class SpyCapturePort(CaptureTextPort):
    def __init__(self) -> None:
        self.commands: list[CaptureTextCommand] = []

    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> None:
        self.commands.append(command)


class FakeMemoryPort:
    """Mirrors the real one-shot mode without touching the database."""

    def __init__(self) -> None:
        self.set_awaiting_calls: list[SetAwaitingMemoryCommand] = []
        self.cancel_calls: list[AccessContext] = []
        self.consume_calls: list[ConsumeMemoryQuestionCommand] = []
        self._armed = False

    async def set_awaiting(
        self, command: SetAwaitingMemoryCommand, transaction: UpdateTransaction
    ) -> None:
        self.set_awaiting_calls.append(command)
        self._armed = True

    async def cancel(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> None:
        self.cancel_calls.append(access_context)
        self._armed = False

    async def consume_question(
        self, command: ConsumeMemoryQuestionCommand, transaction: UpdateTransaction
    ) -> MemoryAskResult | None:
        self.consume_calls.append(command)
        if not self._armed:
            return None
        question = " ".join(command.question.split())
        if not question:
            return MemoryAskResult(question_required=True)
        self._armed = False
        return MemoryAskResult(question_required=False)


@pytest_asyncio.fixture(autouse=True)
async def reset_memory_mode_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=USER_ID,
                role="admin",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=USER_SPACE_ID,
                owner_user_id=USER_ID,
                timezone="Asia/Jerusalem",
                language="ru",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(TelegramIdentity).values(
                id=UUID("00000000-0000-0000-0000-000000000021"),
                telegram_user_id=TELEGRAM_USER_ID,
                user_id=USER_ID,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        TELEGRAM_USER_ID,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        TELEGRAM_USER_ID,
        value,
        telegram_message_id=update_id + 1_000,
    )


def build(
    engine: AsyncEngine,
    memory_port: FakeMemoryPort,
    capture_port: SpyCapturePort,
) -> LocalUpdateProcessor:
    task_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_text_port=capture_port,
        task_mode_port=task_port,
        task_panel_port=task_port,
        exact_search_port=ExactSearchInTransaction(),
        project_panel_port=ProjectContextInTransaction(),
        memory_ask_port=memory_port,
    )


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


@pytest.mark.asyncio
async def test_memory_ask_sets_mode_and_cancels_search(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    await app.process(callback(100, "search:prompt"))
    assert await count(schema_engine, PendingSearchModeModel) == 1

    result = await app.process(callback(101, "memory:ask"))

    assert result.kind is AcknowledgementKind.MEMORY_MODE_SET
    assert len(memory.set_awaiting_calls) == 1
    assert await count(schema_engine, PendingSearchModeModel) == 0


@pytest.mark.asyncio
async def test_next_text_queues_one_question_without_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    await app.process(callback(110, "memory:ask"))
    query = text_update(111, "что я решил про проект?")
    queued = await app.process(query)
    duplicate = await app.process(query)

    assert queued.kind is AcknowledgementKind.MEMORY_QUESTION_QUEUED
    assert duplicate.fresh is False
    assert len(memory.consume_calls) == 1
    assert capture.commands == []
    assert await count(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_blank_question_requires_a_question(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    await app.process(callback(120, "memory:ask"))
    required = await app.process(text_update(121, "   \n "))

    assert required.kind is AcknowledgementKind.MEMORY_QUESTION_REQUIRED
    assert capture.commands == []


@pytest.mark.asyncio
async def test_text_without_mode_falls_through_to_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    captured = await app.process(text_update(130, "обычная заметка"))

    assert captured.kind is AcknowledgementKind.CAPTURED
    assert len(memory.consume_calls) == 1
    assert [command.raw_text for command in capture.commands] == ["обычная заметка"]


@pytest.mark.asyncio
async def test_memory_cancel_clears_mode(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    await app.process(callback(140, "memory:ask"))
    cancelled = await app.process(callback(141, "memory:cancel"))

    assert cancelled.kind is AcknowledgementKind.MEMORY_MODE_CANCELLED
    assert len(memory.cancel_calls) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("other_button", ["search:prompt", "projects:list"])
async def test_other_panel_button_clears_memory_mode(
    engine: AsyncEngine, schema_engine: AsyncEngine, other_button: str
) -> None:
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    await app.process(callback(150, "memory:ask"))
    memory.cancel_calls.clear()
    await app.process(callback(151, other_button))

    # The one-shot mode does not stick: it was cancelled by the other button, so
    # a following text is never treated as a memory question.
    assert len(memory.cancel_calls) >= 1
    followup = await app.process(text_update(152, "это уже обычный текст"))
    assert followup.kind is not AcknowledgementKind.MEMORY_QUESTION_QUEUED

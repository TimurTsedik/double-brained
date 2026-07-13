from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    TelegramUpdateReceipt,
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
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
    TaskProvenanceModel,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    SetAwaitingTaskCommand,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType, TaskStatus
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_task_capture_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="admin",
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
        await connection.execute(
            insert(TelegramIdentity).values(
                id=UUID("00000000-0000-0000-0000-000000000021"),
                telegram_user_id=42,
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=value,
        telegram_message_id=update_id + 1_000,
    )


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    transaction_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        transaction_port,
        transaction_port,
    )


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


@pytest.mark.asyncio
async def test_button_then_text_atomically_creates_source_task_and_provenance(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)

    mode_result = await app.process(callback(100, "task:await_text"))
    task_result = await app.process(text_update(101, "  Купить молоко  "))

    assert mode_result.kind is AcknowledgementKind.TASK_MODE_SET
    assert task_result.kind is AcknowledgementKind.CAPTURED
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TaskModel) == 1
    assert await count(schema_engine, TaskProvenanceModel) == 1
    async with create_session_factory(schema_engine)() as session:
        task = await session.scalar(select(TaskModel))
        source = await session.scalar(select(CaptureEventModel))
        provenance = await session.scalar(select(TaskProvenanceModel))
        mode = await session.scalar(select(PendingCaptureSelectionModel))
    assert task is not None
    assert source is not None
    assert provenance is not None
    assert mode is not None
    assert task.title == "  Купить молоко  "
    assert task.status is TaskStatus.INBOX
    assert task.user_space_id == source.user_space_id == provenance.user_space_id
    assert task.source_capture_event_id == source.id
    assert source.id == provenance.source_capture_event_id
    assert provenance.task_id == task.id
    assert mode.selection is PendingCaptureType.NOTE


@pytest.mark.asyncio
async def test_duplicate_task_text_update_creates_no_second_source_or_task(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(callback(110, "task:await_text"))
    update = text_update(111, "one task")

    first = await app.process(update)
    duplicate = await app.process(update)

    assert first.kind is duplicate.kind is AcknowledgementKind.CAPTURED
    assert first.trace_id == duplicate.trace_id
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TaskModel) == 1
    assert await count(schema_engine, TaskProvenanceModel) == 1


@pytest.mark.asyncio
async def test_duplicate_callback_is_idempotent_and_keeps_one_pending_mode(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    update = callback(115, "task:await_text")

    first = await app.process(update)
    duplicate = await app.process(update)

    assert first.kind is duplicate.kind is AcknowledgementKind.TASK_MODE_SET
    assert first.fresh is True
    assert duplicate.fresh is False
    assert first.trace_id == duplicate.trace_id
    assert await count(schema_engine, PendingCaptureSelectionModel) == 1
    assert await count(schema_engine, TelegramUpdateReceipt) == 1


@pytest.mark.asyncio
async def test_normal_text_without_mode_is_capture_only(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    result = await processor(engine).process(text_update(120, "just remember this"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TaskModel) == 0
    assert await count(schema_engine, TaskProvenanceModel) == 0


@pytest.mark.asyncio
async def test_command_does_not_consume_pending_mode(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(callback(125, "task:await_text"))

    ignored = await app.process(text_update(126, "  /not-a-task"))
    created = await app.process(text_update(127, "still becomes a task"))

    assert ignored.kind is AcknowledgementKind.IGNORED
    assert created.kind is AcknowledgementKind.CAPTURED
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TaskModel) == 1


class FailingAfterTaskTransactionPort(TaskCaptureInTransaction):
    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent:
        await super().capture(command, transaction)
        raise RuntimeError("task transaction failed")


class FailingModeTransactionPort(TaskCaptureInTransaction):
    def __init__(self, action: str) -> None:
        self._action = action

    async def set_awaiting_task(
        self, command: SetAwaitingTaskCommand, transaction: UpdateTransaction
    ) -> None:
        await super().set_awaiting_task(command, transaction)
        if self._action == "set":
            raise RuntimeError("mode set transaction failed")

    async def cancel(
        self, command: CancelPendingTaskCommand, transaction: UpdateTransaction
    ) -> None:
        await super().cancel(command, transaction)
        if self._action == "cancel":
            raise RuntimeError("mode cancel transaction failed")


def processor_with_port(
    engine: AsyncEngine, transaction_port: TaskCaptureInTransaction
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        transaction_port,
        transaction_port,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "failing_action", "expected_before_retry"),
    [
        ("task:await_text", "set", None),
        ("task:cancel", "cancel", PendingCaptureType.TASK),
    ],
)
async def test_failed_mode_callback_rolls_back_and_retry_applies_exactly_once(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    callback_data: str,
    failing_action: str,
    expected_before_retry: PendingCaptureType | None,
) -> None:
    if callback_data == "task:cancel":
        await processor(engine).process(callback(140, "task:await_text"))
    update = callback(141, callback_data)

    with pytest.raises(RuntimeError, match=f"mode {failing_action} transaction failed"):
        await processor_with_port(
            engine, FailingModeTransactionPort(failing_action)
        ).process(update)

    async with create_session_factory(schema_engine)() as session:
        mode_before_retry = await session.scalar(select(PendingCaptureSelectionModel))
    assert (
        None if mode_before_retry is None else mode_before_retry.selection
    ) is expected_before_retry
    assert await count(schema_engine, TelegramUpdateReceipt) == (
        0 if callback_data == "task:await_text" else 1
    )

    retry = await processor(engine).process(update)

    assert retry.kind is (
        AcknowledgementKind.TASK_MODE_SET
        if callback_data == "task:await_text"
        else AcknowledgementKind.TASK_MODE_CANCELLED
    )
    async with create_session_factory(schema_engine)() as session:
        mode_after_retry = await session.scalar(select(PendingCaptureSelectionModel))
    assert mode_after_retry is not None
    assert mode_after_retry.selection is (
        PendingCaptureType.TASK
        if callback_data == "task:await_text"
        else PendingCaptureType.NOTE
    )
    assert await count(schema_engine, TelegramUpdateReceipt) == (
        1 if callback_data == "task:await_text" else 2
    )


@pytest.mark.asyncio
async def test_failure_rolls_back_mode_source_task_and_receipt_then_retry_succeeds(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    mode_port = TaskCaptureInTransaction()
    setting_processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        mode_port,
        mode_port,
    )
    await setting_processor.process(callback(130, "task:await_text"))
    update = text_update(131, "retry this task")
    failing_port = FailingAfterTaskTransactionPort()
    failing_processor = LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        failing_port,
        failing_port,
    )

    with pytest.raises(RuntimeError, match="task transaction failed"):
        await failing_processor.process(update)

    assert await count(schema_engine, CaptureEventModel) == 0
    assert await count(schema_engine, TaskModel) == 0
    assert await count(schema_engine, TaskProvenanceModel) == 0
    assert await count(schema_engine, TelegramUpdateReceipt) == 1
    async with create_session_factory(schema_engine)() as session:
        mode = await session.scalar(select(PendingCaptureSelectionModel))
    assert mode is not None
    assert mode.selection is PendingCaptureType.TASK

    retry = await processor(engine).process(update)

    assert retry.kind is AcknowledgementKind.CAPTURED
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TaskModel) == 1
    assert await count(schema_engine, TaskProvenanceModel) == 1
    assert await count(schema_engine, TelegramUpdateReceipt) == 2

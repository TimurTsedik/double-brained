from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.exact_search_in_transaction import (
    ExactSearchInTransaction,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
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
from second_brain.slices.identity.application.contracts import UpdateTransaction
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from second_brain.slices.retrieval.application.contracts import (
    ConsumeSearchQueryCommand,
    SearchPanelResult,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_telegram_search_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=USER_ID,
                role="member",
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
                telegram_user_id=42,
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
        42,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        value,
        telegram_message_id=update_id + 1_000,
    )


def processor(
    engine: AsyncEngine,
    search_port: ExactSearchInTransaction | None = None,
) -> LocalUpdateProcessor:
    task_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_port,
        task_port,
        task_port,
        search_port or ExactSearchInTransaction(),
    )


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


@pytest.mark.asyncio
async def test_search_query_returns_typed_result_without_creating_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(text_update(300, "Remember PostgreSQL exact search"))
    prompted = await app.process(callback(301, "search:prompt"))
    query_update = text_update(302, "postgres")

    searched = await app.process(query_update)
    duplicate = await app.process(query_update)

    assert prompted.kind is AcknowledgementKind.SEARCH_MODE_SET
    assert searched.kind is AcknowledgementKind.SEARCH_COMPLETED
    assert searched.search_panel is not None
    assert searched.search_panel.query_required is False
    assert len(searched.search_panel.items) == 1
    assert searched.search_panel.items[0].record_type is SearchRecordType.NOTE
    assert searched.search_panel.items[0].text == "Remember PostgreSQL exact search"
    assert duplicate.kind is AcknowledgementKind.SEARCH_COMPLETED
    assert duplicate.fresh is False
    assert duplicate.search_panel is None
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, NoteModel) == 1
    assert await count(schema_engine, PendingSearchModeModel) == 0
    assert await count(schema_engine, TelegramUpdateReceipt) == 3


@pytest.mark.asyncio
async def test_whitespace_keeps_search_pending_until_valid_query(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(callback(310, "search:prompt"))

    required = await app.process(text_update(311, "   \n "))
    completed = await app.process(text_update(312, "nothing"))

    assert required.kind is AcknowledgementKind.SEARCH_QUERY_REQUIRED
    assert required.search_panel == SearchPanelResult((), query_required=True)
    assert completed.kind is AcknowledgementKind.SEARCH_COMPLETED
    assert completed.search_panel == SearchPanelResult((), query_required=False)
    assert await count(schema_engine, PendingSearchModeModel) == 0
    assert await count(schema_engine, CaptureEventModel) == 0


class FailingAfterSearchPort(ExactSearchInTransaction):
    async def consume_query(
        self,
        command: ConsumeSearchQueryCommand,
        transaction: UpdateTransaction,
    ) -> SearchPanelResult | None:
        result = await super().consume_query(command, transaction)
        if result is not None:
            raise RuntimeError("search transaction failed")
        return result


@pytest.mark.asyncio
async def test_search_failure_rolls_back_pending_state_and_receipt_then_retries(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    normal = processor(engine)
    await normal.process(text_update(320, "PostgreSQL survives retry"))
    await normal.process(callback(321, "search:prompt"))
    query_update = text_update(322, "postgres")

    with pytest.raises(RuntimeError, match="search transaction failed"):
        await processor(engine, FailingAfterSearchPort()).process(query_update)

    assert await count(schema_engine, PendingSearchModeModel) == 1
    assert await count(schema_engine, TelegramUpdateReceipt) == 2

    retried = await normal.process(query_update)

    assert retried.kind is AcknowledgementKind.SEARCH_COMPLETED
    assert retried.search_panel is not None
    assert [item.text for item in retried.search_panel.items] == [
        "PostgreSQL survives retry"
    ]
    assert await count(schema_engine, PendingSearchModeModel) == 0
    assert await count(schema_engine, TelegramUpdateReceipt) == 3

"""E2E показа записи целиком: callback → транзакция → RLS → transient payload.

Живая цепочка LocalUpdateProcessor + RecordViewInTransaction на PostgreSQL:
receipt пишет result_kind='record_shown', дата заголовка приходит в часовом
поясе пространства, чужой/несуществующий/мусорный callback → IGNORED без
payload'а, replay дубля молчит (fresh=False, частей нет).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.record_view_in_transaction import RecordViewInTransaction
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
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.test_semantic_index_persistence import (
    chunks_command,
    make_chunk,
    store_chunks,
    vector_of,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")
FOREIGN_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
FOREIGN_SPACE_ID = UUID("00000000-0000-0000-0000-000000000012")
TRACE_ID = "1" * 32


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_show_record_schema(
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
                    "id": USER_ID,
                    "role": "admin",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": FOREIGN_USER_ID,
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
                    "id": USER_SPACE_ID,
                    "owner_user_id": USER_ID,
                    "timezone": "Asia/Jerusalem",
                    "language": "ru",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": FOREIGN_SPACE_ID,
                    "owner_user_id": FOREIGN_USER_ID,
                    "timezone": "Asia/Jerusalem",
                    "language": "ru",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
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


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    task_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        capture_text_port=task_port,
        task_mode_port=task_port,
        task_panel_port=task_port,
        record_view_port=RecordViewInTransaction(),
    )


async def captured_note(schema_engine: AsyncEngine, text: str) -> tuple[UUID, UUID]:
    async with create_session_factory(schema_engine)() as session:
        row = (
            await session.execute(
                select(NoteModel.id, NoteModel.source_capture_event_id).where(
                    NoteModel.text == text
                )
            )
        ).one()
        return row.id, row.source_capture_event_id


async def stored_result_kind(schema_engine: AsyncEngine, update_id: int) -> str:
    async with create_session_factory(schema_engine)() as session:
        kind = await session.scalar(
            select(TelegramUpdateReceipt.result_kind).where(
                TelegramUpdateReceipt.update_id == update_id
            )
        )
        assert kind is not None
        return kind


@pytest.mark.asyncio
async def test_show_callback_returns_the_full_record_in_the_space_timezone(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(text_update(600, "Полный текст заметки для показа"))
    note_id, _ = await captured_note(schema_engine, "Полный текст заметки для показа")

    shown = await app.process(callback(601, f"show:note:{note_id}"))

    assert shown.kind is AcknowledgementKind.RECORD_SHOWN
    assert shown.fresh is True
    assert shown.record_view is not None
    record = shown.record_view.record
    assert record.text == "Полный текст заметки для показа"
    assert record.record_type is SearchRecordType.NOTE
    assert record.created_at == NOW
    # Дата заголовка — в часовом поясе пространства (Asia/Jerusalem, летом +3).
    assert record.created_at.utcoffset() == timedelta(hours=3)
    # Непроиндексированная запись: секции похожего нет.
    assert shown.record_view.related == ()
    assert await stored_result_kind(schema_engine, 601) == "record_shown"


@pytest.mark.asyncio
async def test_show_callback_replay_stays_silent(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(text_update(610, "Заметка для replay"))
    note_id, _ = await captured_note(schema_engine, "Заметка для replay")
    show = callback(611, f"show:note:{note_id}")

    fresh = await app.process(show)
    replay = await app.process(show)

    assert fresh.kind is AcknowledgementKind.RECORD_SHOWN
    assert fresh.record_view is not None
    assert replay.kind is AcknowledgementKind.RECORD_SHOWN
    assert replay.fresh is False
    assert replay.record_view is None


@pytest.mark.asyncio
async def test_show_related_walks_to_the_semantic_neighbour(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(text_update(620, "Первая заметка про кофе"))
    await app.process(text_update(621, "Вторая заметка про кофе"))
    first_id, first_capture = await captured_note(
        schema_engine, "Первая заметка про кофе"
    )
    second_id, second_capture = await captured_note(
        schema_engine, "Вторая заметка про кофе"
    )
    access = AccessContext(USER_ID, USER_SPACE_ID)
    await store_chunks(
        engine,
        chunks_command(
            access,
            record_id=first_id,
            capture_event_id=first_capture,
            chunks=(make_chunk(0, "первая", vector_of(1.0)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            access,
            record_id=second_id,
            capture_event_id=second_capture,
            chunks=(make_chunk(0, "вторая", vector_of(0.6, 0.8)),),
        ),
    )

    shown = await app.process(callback(622, f"show:note:{first_id}"))

    assert shown.record_view is not None
    assert [(record.id, record.text) for record in shown.record_view.related] == [
        (second_id, "Вторая заметка про кофе")
    ]


@pytest.mark.asyncio
async def test_foreign_unknown_and_garbage_uuid_are_all_ignored(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    foreign_note_id = uuid4()
    foreign_capture_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=foreign_capture_id,
                user_space_id=FOREIGN_SPACE_ID,
                channel="telegram",
                bot_id=100,
                telegram_update_id=999,
                telegram_message_id=10_999,
                raw_text="чужой захват",
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
        await connection.execute(
            insert(NoteModel).values(
                id=foreign_note_id,
                user_space_id=FOREIGN_SPACE_ID,
                text="чужая заметка",
                source_capture_event_id=foreign_capture_id,
                created_at=NOW,
                updated_at=NOW,
                trace_id=TRACE_ID,
            )
        )

    foreign = await app.process(callback(630, f"show:note:{foreign_note_id}"))
    unknown = await app.process(callback(631, f"show:note:{uuid4()}"))
    garbage = await app.process(callback(632, "show:note:garbage"))

    for result, update_id in ((foreign, 630), (unknown, 631), (garbage, 632)):
        assert result.kind is AcknowledgementKind.IGNORED
        assert result.record_view is None
        assert await stored_result_kind(schema_engine, update_id) == "ignored"

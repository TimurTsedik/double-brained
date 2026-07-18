"""Захват текста со ссылками: sidecar record_urls в ТОЙ ЖЕ транзакции.

Текст пользователя неприкосновенен (компас волны): в записи он остаётся
дословным, ссылки живут рядом в record_urls, а очередь титулов page_titles
пополняется идемпотентно (pending) в том же коммите захвата.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.application.contracts import TelegramLink
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
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.weblinks.adapters.persistence.models import (
    PageTitleModel,
    RecordUrlModel,
)
from second_brain.slices.weblinks.domain.entities import (
    PageTitleStatus,
    WeblinkRecordKind,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")
RAW_TEXT = "смотри доклад тут и ещё https://b.example/Page?x=1"


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_weblink_schema(
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
                id=SPACE_ID,
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


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    transaction_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        transaction_port,
        transaction_port,
        transaction_port,
    )


def text_update(
    update_id: int, value: str, links: tuple[TelegramLink, ...]
) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=value,
        telegram_message_id=update_id + 1_000,
        links=links,
    )


LINKS = (
    TelegramLink(label="тут", url="https://a.example/Talk"),
    TelegramLink(label="https://b.example/Page?x=1", url="https://b.example/Page?x=1"),
)


@pytest.mark.asyncio
async def test_capture_with_links_writes_sidecar_rows_in_the_same_commit(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    result = await processor(engine).process(text_update(100, RAW_TEXT, LINKS))

    assert result.kind is AcknowledgementKind.CAPTURED
    async with create_session_factory(schema_engine)() as session:
        note = await session.scalar(select(NoteModel))
        source = await session.scalar(select(CaptureEventModel))
        urls = (
            await session.scalars(
                select(RecordUrlModel).order_by(RecordUrlModel.position)
            )
        ).all()
        titles = (
            await session.scalars(
                select(PageTitleModel).order_by(PageTitleModel.normalized_url)
            )
        ).all()
    assert note is not None and source is not None
    # Текст записи и журнала — ДОСЛОВНО присланный, ничего не вшито.
    assert note.text == RAW_TEXT
    assert source.raw_text == RAW_TEXT
    # Sidecar: упорядоченные пары «слово → адрес» с видом и id фактической записи.
    assert [
        (row.record_kind, row.record_id, row.position, row.label, row.url)
        for row in urls
    ] == [
        (WeblinkRecordKind.NOTE, note.id, 0, "тут", "https://a.example/Talk"),
        (
            WeblinkRecordKind.NOTE,
            note.id,
            1,
            "https://b.example/Page?x=1",
            "https://b.example/Page?x=1",
        ),
    ]
    assert all(row.user_space_id == SPACE_ID for row in urls)
    assert all(row.trace_id == source.trace_id for row in urls)
    # Очередь титулов: по одной pending-строке на нормализованный URL.
    assert [(row.original_url, row.normalized_url, row.status) for row in titles] == [
        ("https://a.example/Talk", "https://a.example/Talk", PageTitleStatus.PENDING),
        (
            "https://b.example/Page?x=1",
            "https://b.example/Page?x=1",
            PageTitleStatus.PENDING,
        ),
    ]
    assert all(row.title is None and row.attempt_count == 0 for row in titles)


@pytest.mark.asyncio
async def test_duplicate_urls_enqueue_one_page_title_row(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Один и тот же адрес в разных записях (и с разным регистром хоста) —
    # одна строка очереди: конфликт по (user_space_id, normalized_url) гасится.
    app = processor(engine)
    await app.process(
        text_update(
            110,
            "раз https://dup.example/a",
            (TelegramLink(label="x", url="https://dup.example/a"),),
        )
    )
    await app.process(
        text_update(
            111,
            "два HTTPS://DUP.example/a ещё",
            (TelegramLink(label="y", url="HTTPS://DUP.example/a"),),
        )
    )

    async with create_session_factory(schema_engine)() as session:
        url_count = await session.scalar(
            select(func.count()).select_from(RecordUrlModel)
        )
        titles = (await session.scalars(select(PageTitleModel))).all()
    assert url_count == 2
    assert len(titles) == 1
    assert titles[0].normalized_url == "https://dup.example/a"
    # original_url — как прислан ПЕРВЫМ.
    assert titles[0].original_url == "https://dup.example/a"


@pytest.mark.asyncio
async def test_text_without_links_writes_no_sidecar_rows(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await processor(engine).process(text_update(120, "просто заметка", ()))

    async with create_session_factory(schema_engine)() as session:
        url_count = await session.scalar(
            select(func.count()).select_from(RecordUrlModel)
        )
        title_count = await session.scalar(
            select(func.count()).select_from(PageTitleModel)
        )
    assert url_count == 0
    assert title_count == 0

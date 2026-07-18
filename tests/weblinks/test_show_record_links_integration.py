"""E2E «показать целиком» со ссылками: record_urls + page_titles → payload.

Живая цепочка LocalUpdateProcessor + RecordViewInTransaction на PostgreSQL:
захват со ссылками пишет sidecar, показ отдаёт links в порядке position,
title подтягивается ТОЛЬКО для fetched-строк своего пространства.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.record_view_in_transaction import RecordViewInTransaction
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
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
from second_brain.slices.weblinks.adapters.persistence.models import PageTitleModel
from second_brain.slices.weblinks.domain.entities import PageTitleStatus
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_show_links_schema(
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


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    task_port = TaskCaptureInTransaction()
    record_view = RecordViewInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        capture_text_port=task_port,
        task_mode_port=task_port,
        task_panel_port=task_port,
        record_view_port=record_view,
        record_links_port=record_view,
    )


@pytest.mark.asyncio
async def test_show_full_returns_links_in_order_with_the_fetched_title(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    capture = await app.process(
        TelegramUpdate(
            bot_id=1,
            update_id=100,
            is_private=True,
            telegram_user_id=42,
            text="доклад тут и сырой https://b.example/raw",
            telegram_message_id=1_100,
            links=(
                TelegramLink(label="тут", url="https://a.example/Talk"),
                TelegramLink(
                    label="https://b.example/raw", url="https://b.example/raw"
                ),
            ),
        )
    )
    assert capture.kind is AcknowledgementKind.CAPTURED

    # Воркер «уже сходил»: первая ссылка получила title.
    async with schema_engine.begin() as connection:
        await connection.execute(
            update(PageTitleModel)
            .where(PageTitleModel.normalized_url == "https://a.example/Talk")
            .values(
                status=PageTitleStatus.FETCHED.value,
                title="Заголовок доклада",
                fetched_at=NOW,
            )
        )
    async with create_session_factory(schema_engine)() as session:
        note_id = (await session.scalars(select(NoteModel.id))).one()

    shown = await app.process(
        TelegramUpdate(
            bot_id=1,
            update_id=101,
            is_private=True,
            telegram_user_id=42,
            text=None,
            callback_query_id="callback-101",
            callback_data=f"show:note:{note_id}",
        )
    )

    assert shown.kind is AcknowledgementKind.RECORD_SHOWN
    assert shown.record_view is not None
    assert [(link.label, link.url, link.title) for link in shown.record_view.links] == [
        ("тут", "https://a.example/Talk", "Заголовок доклада"),
        ("https://b.example/raw", "https://b.example/raw", None),
    ]
    # Текст записи в payload — дословный.
    assert shown.record_view.record.text == "доклад тут и сырой https://b.example/raw"

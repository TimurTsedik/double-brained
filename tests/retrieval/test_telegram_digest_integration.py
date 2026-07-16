"""E2E сводки: callback → транзакция → RLS → transient payload → «⬇️ Ещё».

Живая цепочка LocalUpdateProcessor + DigestInTransaction на PostgreSQL: receipt
пишет result_kind, страницы листаются реальным callback'ом «Ещё» из клавиатуры
(offset = фактически отрендеренные строки, as_of — снимок первого клика), запись,
созданная ПОСЛЕ as_of, не появляется и не сдвигает счётчики, чужие записи не
видны в обе стороны, replay дубля молчит.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.digest_in_transaction import DigestInTransaction
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
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
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.retrieval.application.contracts import DigestPage
from tests.identity.conftest import IsolatedDatabase
from tests.identity.locale_fakes import FakeLocaleResolver

TZ = ZoneInfo("Asia/Jerusalem")
# Среда 15.07.2026 12:00 UTC; неделя пространства — с 13.07 00:00+03 (12.07 21:00 UTC).
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WEEK_START_UTC = datetime(2026, 7, 12, 21, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")
FOREIGN_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
FOREIGN_SPACE_ID = UUID("00000000-0000-0000-0000-000000000012")
ACCESS = AccessContext(USER_ID, USER_SPACE_ID)
FOREIGN_ACCESS = AccessContext(FOREIGN_USER_ID, FOREIGN_SPACE_ID)
TRACE_ID = "1" * 32


class FixedClock:
    def now(self) -> datetime:
        return NOW


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)


@pytest_asyncio.fixture(autouse=True)
async def reset_digest_schema(
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
            insert(TelegramIdentity),
            [
                {
                    "id": UUID("00000000-0000-0000-0000-000000000021"),
                    "telegram_user_id": 42,
                    "user_id": USER_ID,
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": UUID("00000000-0000-0000-0000-000000000022"),
                    "telegram_user_id": 43,
                    "user_id": FOREIGN_USER_ID,
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )


def callback(update_id: int, data: str, telegram_user_id: int = 42) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        telegram_user_id,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        digest_port=DigestInTransaction(),
    )


_SEED_SEQUENCE = iter(range(1, 1_000_000))


async def seed_notes(
    schema_engine: AsyncEngine,
    access: AccessContext,
    texts_and_dates: list[tuple[str, datetime]],
) -> dict[str, UUID]:
    capture_id = uuid4()
    delivery = next(_SEED_SEQUENCE)
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=delivery,
                telegram_message_id=10_000 + delivery,
                raw_text="digest seed",
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
        ids: dict[str, UUID] = {}
        for text, created_at in texts_and_dates:
            note_id = uuid4()
            ids[text] = note_id
            await connection.execute(
                insert(NoteModel).values(
                    id=note_id,
                    user_space_id=access.user_space_id,
                    text=text,
                    source_capture_event_id=capture_id,
                    created_at=created_at,
                    updated_at=created_at,
                    trace_id=TRACE_ID,
                )
            )
    return ids


async def stored_result_kind(schema_engine: AsyncEngine, update_id: int) -> str:
    async with create_session_factory(schema_engine)() as session:
        kind = await session.scalar(
            select(TelegramUpdateReceipt.result_kind).where(
                TelegramUpdateReceipt.update_id == update_id
            )
        )
        assert kind is not None
        return kind


async def render_more_callback(page: DigestPage) -> str | None:
    """Рендерит страницу реальным гейтвеем и достаёт callback кнопки «Ещё»."""
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver(Locale.RU)
    )
    await gateway.send_digest(callback(0, "digest:week"), page)
    message = bot.sent_messages[0]
    if "reply_markup" not in message:
        return None
    last_row = message["reply_markup"].inline_keyboard[-1]
    data = last_row[0].callback_data
    if isinstance(data, str) and data.startswith("digest:more:"):
        return data
    return None


@pytest.mark.asyncio
async def test_period_click_stores_the_receipt_and_returns_the_page(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_notes(
        schema_engine,
        ACCESS,
        [("свежая заметка", NOW - timedelta(hours=1))],
    )
    app = processor(engine)

    menu = await app.process(callback(700, "digest:menu"))
    shown = await app.process(callback(701, "digest:week"))

    assert menu.kind is AcknowledgementKind.DIGEST_MENU_SHOWN
    assert await stored_result_kind(schema_engine, 700) == "digest_menu_shown"
    assert shown.kind is AcknowledgementKind.DIGEST_SHOWN
    assert shown.fresh is True
    assert await stored_result_kind(schema_engine, 701) == "digest_shown"
    page = shown.digest_page
    assert page is not None
    assert page.total == 1
    assert [item.text for item in page.items] == ["свежая заметка"]
    # Даты — в поясе пространства; снимок равен «сейчас» с точностью до секунды.
    assert page.items[0].created_at.utcoffset() == timedelta(hours=3)
    assert page.as_of == NOW.astimezone(TZ)
    assert page.period_start.astimezone(UTC) == WEEK_START_UTC


@pytest.mark.asyncio
async def test_25_records_page_through_10_10_5_via_the_real_more_button(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_notes(
        schema_engine,
        ACCESS,
        [
            (f"note {number:02d}", NOW - timedelta(minutes=number))
            for number in range(25)
        ],
    )
    app = processor(engine)

    first = await app.process(callback(710, "digest:week"))
    assert first.digest_page is not None
    more_1 = await render_more_callback(first.digest_page)
    assert more_1 == f"digest:more:week:10:{int(NOW.timestamp())}"

    second = await app.process(callback(711, more_1))
    assert second.kind is AcknowledgementKind.DIGEST_SHOWN
    assert second.digest_page is not None
    more_2 = await render_more_callback(second.digest_page)
    assert more_2 == f"digest:more:week:20:{int(NOW.timestamp())}"

    third = await app.process(callback(712, more_2))
    assert third.digest_page is not None
    assert await render_more_callback(third.digest_page) is None

    pages = (first.digest_page, second.digest_page, third.digest_page)
    assert [len(page.items) for page in pages] == [10, 10, 5]
    assert all(page.total == 25 for page in pages)
    texts = [item.text for page in pages for item in page.items]
    assert texts == [f"note {number:02d}" for number in range(25)]


@pytest.mark.asyncio
async def test_a_record_created_after_as_of_never_appears_in_the_snapshot(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_notes(
        schema_engine,
        ACCESS,
        [
            (f"note {number:02d}", NOW - timedelta(minutes=1 + number))
            for number in range(12)
        ],
    )
    app = processor(engine)
    first = await app.process(callback(720, "digest:week"))
    assert first.digest_page is not None
    more = await render_more_callback(first.digest_page)
    assert more is not None

    # Запись появляется МЕЖДУ страницами, но позже снимка as_of.
    late_ids = await seed_notes(
        schema_engine, ACCESS, [("создана после снимка", NOW + timedelta(seconds=30))]
    )

    second = await app.process(callback(721, more))
    assert second.digest_page is not None

    assert second.digest_page.total == first.digest_page.total == 12
    assert first.digest_page.counters == second.digest_page.counters
    pages = (first.digest_page, second.digest_page)
    listed = {item.id for page in pages for item in page.items}
    assert late_ids["создана после снимка"] not in listed
    assert len(listed) == 12


@pytest.mark.asyncio
async def test_digest_never_shows_foreign_records_in_either_direction(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    own = await seed_notes(
        schema_engine, ACCESS, [("приватно A", NOW - timedelta(hours=1))]
    )
    foreign = await seed_notes(
        schema_engine, FOREIGN_ACCESS, [("приватно B", NOW - timedelta(hours=1))]
    )
    app = processor(engine)

    page_a = (await app.process(callback(730, "digest:week"))).digest_page
    page_b = (
        await app.process(callback(731, "digest:week", telegram_user_id=43))
    ).digest_page

    assert page_a is not None and page_b is not None
    assert [item.id for item in page_a.items] == [own["приватно A"]]
    assert [item.id for item in page_b.items] == [foreign["приватно B"]]
    assert page_a.total == 1
    assert page_b.total == 1


@pytest.mark.asyncio
async def test_replay_of_the_same_digest_click_stays_silent(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_notes(schema_engine, ACCESS, [("заметка", NOW - timedelta(hours=1))])
    app = processor(engine)
    click = callback(740, "digest:week")

    fresh = await app.process(click)
    replay = await app.process(click)

    assert fresh.kind is AcknowledgementKind.DIGEST_SHOWN
    assert fresh.digest_page is not None
    assert replay.kind is AcknowledgementKind.DIGEST_SHOWN
    assert replay.fresh is False
    assert replay.digest_page is None


@pytest.mark.asyncio
async def test_spoofed_and_garbage_digest_callbacks_are_ignored(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_notes(schema_engine, ACCESS, [("заметка", NOW - timedelta(hours=1))])
    app = processor(engine)
    as_of_unix = int(NOW.timestamp())

    past_end = await app.process(callback(750, f"digest:more:week:999999:{as_of_unix}"))
    garbage = await app.process(callback(751, "digest:more:week:-1:5"))
    unknown_period = await app.process(callback(752, "digest:decade"))

    for result, update_id in ((past_end, 750), (garbage, 751), (unknown_period, 752)):
        assert result.kind is AcknowledgementKind.IGNORED
        assert result.digest_page is None
        assert await stored_result_kind(schema_engine, update_id) == "ignored"


@pytest.mark.asyncio
async def test_empty_period_returns_an_honest_empty_page(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Единственная запись — ДО начала недели.
    await seed_notes(
        schema_engine,
        ACCESS,
        [("прошлая неделя", WEEK_START_UTC - timedelta(hours=2))],
    )
    app = processor(engine)

    shown = await app.process(callback(760, "digest:week"))

    assert shown.kind is AcknowledgementKind.DIGEST_SHOWN
    page = shown.digest_page
    assert page is not None
    assert page.total == 0
    assert page.items == ()

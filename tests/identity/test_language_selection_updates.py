"""Флоу выбора и смены языка через LocalUpdateProcessor (Task 6).

Проверяется реальным PostgreSQL (те же прод-классы), потому что запись языка,
forward-only гейт и снятие awaiting-режимов должны быть видны в ТОЙ ЖЕ
ingress-транзакции — фейк не докажет транзакционность.
"""

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
    PostgresEnrollmentRepository,
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.identity.application.enrollment import CreateEnrollmentInvite
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
PEPPER = b"lang-flow-pepper"
PEPPER_KEY_ID = "lang-key"


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
    def __init__(self) -> None:
        self.cancel_calls: list[AccessContext] = []
        self._armed = False

    async def set_awaiting(
        self, command: SetAwaitingMemoryCommand, transaction: UpdateTransaction
    ) -> None:
        self._armed = True

    async def cancel(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> None:
        self.cancel_calls.append(access_context)
        self._armed = False

    async def consume_question(
        self, command: ConsumeMemoryQuestionCommand, transaction: UpdateTransaction
    ) -> MemoryAskResult | None:
        if not self._armed:
            return None
        self._armed = False
        return MemoryAskResult(question_required=False)


@pytest_asyncio.fixture(autouse=True)
async def reset_language_flow_schema(
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


async def set_language(schema_engine: AsyncEngine, language: str | None) -> None:
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            space = await session.get(UserSpace, USER_SPACE_ID)
            assert space is not None
            space.language = language
            space.updated_at = NOW


async def read_language(schema_engine: AsyncEngine) -> str | None:
    async with create_session_factory(schema_engine)() as session:
        return await session.scalar(
            select(UserSpace.language).where(UserSpace.id == USER_SPACE_ID)
        )


async def read_updated_at(schema_engine: AsyncEngine) -> datetime:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(
            select(UserSpace.updated_at).where(UserSpace.id == USER_SPACE_ID)
        )
        assert value is not None
        return value


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


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


def plain_start(update_id: int) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=TELEGRAM_USER_ID,
        text="/start",
    )


def build(
    engine: AsyncEngine, memory_port: FakeMemoryPort, capture_port: SpyCapturePort
) -> LocalUpdateProcessor:
    task_port = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=PEPPER,
        pepper_key_id=PEPPER_KEY_ID,
        capture_text_port=capture_port,
        task_mode_port=task_port,
        task_panel_port=task_port,
        exact_search_port=ExactSearchInTransaction(),
        project_panel_port=ProjectContextInTransaction(),
        memory_ask_port=memory_port,
    )


# ---------------------------------------------------------------------------
# (а) enrollment → chooser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrollment_shows_language_chooser(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Fresh DB: drop the seeded owner so enrollment can admit a new one.
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            await session.execute(
                TelegramIdentity.__table__.delete().where(
                    TelegramIdentity.telegram_user_id == TELEGRAM_USER_ID
                )
            )
            await session.execute(
                UserSpace.__table__.delete().where(UserSpace.id == USER_SPACE_ID)
            )
            await session.execute(User.__table__.delete().where(User.id == USER_ID))

    invite = await CreateEnrollmentInvite(
        repository=PostgresEnrollmentRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=PEPPER,
        pepper_key_id=PEPPER_KEY_ID,
    ).execute()

    app = build(engine, FakeMemoryPort(), SpyCapturePort())
    result = await app.process(
        TelegramUpdate(
            bot_id=1,
            update_id=1,
            is_private=True,
            telegram_user_id=999,
            text=f"/start {invite.token}",
        )
    )

    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN


# ---------------------------------------------------------------------------
# (б) forward-only bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_bridge_shows_chooser_on_text_when_language_null(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, None)
    capture = SpyCapturePort()
    app = build(engine, FakeMemoryPort(), capture)

    result = await app.process(text_update(10, "обычная заметка"))

    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
    assert capture.commands == []


@pytest.mark.asyncio
async def test_forward_bridge_shows_chooser_on_start_when_language_null(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, None)
    app = build(engine, FakeMemoryPort(), SpyCapturePort())

    result = await app.process(plain_start(11))

    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN


@pytest.mark.asyncio
async def test_forward_bridge_shows_chooser_on_callback_when_language_null(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, None)
    app = build(engine, FakeMemoryPort(), SpyCapturePort())

    result = await app.process(callback(12, "tasks:list"))

    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN


@pytest.mark.asyncio
async def test_no_chooser_when_language_chosen(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, "ru")
    capture = SpyCapturePort()
    app = build(engine, FakeMemoryPort(), capture)

    result = await app.process(text_update(13, "обычная заметка"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert [command.raw_text for command in capture.commands] == ["обычная заметка"]


# ---------------------------------------------------------------------------
# gate exception: lang:* callbacks pass through even when language is null
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lang_callback_is_not_blocked_by_the_gate(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, None)
    app = build(engine, FakeMemoryPort(), SpyCapturePort())

    result = await app.process(callback(20, "lang:en"))

    assert result.kind is AcknowledgementKind.LANGUAGE_SELECTED
    assert await read_language(schema_engine) == "en"


# ---------------------------------------------------------------------------
# lang:menu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lang_menu_shows_chooser_when_language_chosen(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, "ru")
    app = build(engine, FakeMemoryPort(), SpyCapturePort())

    result = await app.process(callback(21, "lang:menu"))

    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
    # Menu does not change the stored language.
    assert await read_language(schema_engine) == "ru"


# ---------------------------------------------------------------------------
# language written in the same ingress transaction, updated_at bumped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lang_selection_writes_language_and_bumps_updated_at(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, None)
    app = build(engine, FakeMemoryPort(), SpyCapturePort())

    result = await app.process(callback(22, "lang:ru"))

    assert result.kind is AcknowledgementKind.LANGUAGE_SELECTED
    assert await read_language(schema_engine) == "ru"
    assert await read_updated_at(schema_engine) == NOW


# ---------------------------------------------------------------------------
# awaiting modes are cleared on selection (no sticking)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lang_selection_clears_awaiting_modes(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, "ru")
    memory = FakeMemoryPort()
    capture = SpyCapturePort()
    app = build(engine, memory, capture)

    await app.process(callback(30, "search:prompt"))
    assert await count(schema_engine, PendingSearchModeModel) == 1
    await app.process(callback(31, "memory:ask"))
    memory.cancel_calls.clear()

    selected = await app.process(callback(32, "lang:en"))

    assert selected.kind is AcknowledgementKind.LANGUAGE_SELECTED
    assert await count(schema_engine, PendingSearchModeModel) == 0
    assert len(memory.cancel_calls) >= 1
    # A following text is a capture, not a stuck memory question or search query.
    followup = await app.process(text_update(33, "уже обычный текст"))
    assert followup.kind is AcknowledgementKind.CAPTURED


# ---------------------------------------------------------------------------
# idempotency: repeated update does not double-write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_lang_selection_is_idempotent(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, "en")
    app = build(engine, FakeMemoryPort(), SpyCapturePort())
    choice = callback(40, "lang:ru")

    first = await app.process(choice)
    # Move the wall clock forward would change updated_at IF re-written.
    after_first = await read_updated_at(schema_engine)
    duplicate = await app.process(choice)

    assert first.kind is AcknowledgementKind.LANGUAGE_SELECTED
    assert duplicate.fresh is False
    assert await read_language(schema_engine) == "ru"
    assert await read_updated_at(schema_engine) == after_first


# ---------------------------------------------------------------------------
# later change applies immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_language_change_applies_immediately(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await set_language(schema_engine, "en")
    app = build(engine, FakeMemoryPort(), SpyCapturePort())

    result = await app.process(callback(50, "lang:ru"))

    assert result.kind is AcknowledgementKind.LANGUAGE_SELECTED
    assert await read_language(schema_engine) == "ru"

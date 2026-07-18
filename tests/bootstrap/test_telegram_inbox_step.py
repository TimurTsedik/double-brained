"""Inbox-шаг воркера: строгий порядок, бэкофф/failed, паритет с поллером.

Шаг клеймит головную строку INBOX (min update_id среди pending бота), гонит
payload через СУЩЕСТВУЮЩИЙ конвейер (normalize → LocalUpdateProcessor →
TelegramPresenter) и повторяет side-эффекты поллера: best-effort
answer_callback ДО обработки и debounce-досылку панели ПОСЛЕ. Ответы
пользователю — байт-в-байт с поллер-путём (сравнение на seam гейтвея).
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.types import Update
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.telegram_inbox_step import TelegramInboxStep
from second_brain.bootstrap.update_processing import build_update_processor
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.inbox import (
    PostgresTelegramInboxQueue,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    TelegramUpdateInbox,
    TelegramUpdateReceipt,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import (
    normalize_aiogram_update,
)
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.identity.domain.entities import TelegramInboxStatus
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 18, 11, 30, tzinfo=UTC)
BOT_ID = 800
ACTOR = 42
MAX_ATTEMPTS = 2
BACKOFF = timedelta(seconds=60)
TRACE_ID = "6" * 32
TINY_DELAY = 0.05
SETTLE = 0.15

USER_ID = uuid4()
USER_SPACE_ID = uuid4()


@pytest_asyncio.fixture(autouse=True)
async def reset_inbox_step_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


async def seed_known_actor(schema_engine: AsyncEngine) -> None:
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
                id=uuid4(),
                telegram_user_id=ACTOR,
                user_id=USER_ID,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def text_payload(update_id: int, text: str = "запомнить это") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 1000,
            "date": 1784000000,
            "chat": {"id": ACTOR, "type": "private", "first_name": "Т"},
            "from": {"id": ACTOR, "is_bot": False, "first_name": "Т"},
            "text": text,
        },
    }


def callback_payload(update_id: int, data: str = "tasks:list") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-panel",
            "from": {"id": ACTOR, "is_bot": False, "first_name": "Т"},
            "chat_instance": "ci-1",
            "data": data,
            "message": {
                "message_id": 77,
                "date": 1784000100,
                "chat": {"id": ACTOR, "type": "private", "first_name": "Т"},
                "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                "text": "панель",
            },
        },
    }


async def enqueue(
    engine: AsyncEngine, payload: dict[str, Any], received_at: datetime
) -> None:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        update_id = payload["update_id"]
        assert isinstance(update_id, int)
        await PostgresTelegramInboxQueue(session).enqueue(
            bot_id=BOT_ID,
            update_id=update_id,
            payload=payload,
            received_at=received_at,
            trace_id=TRACE_ID,
        )


async def inbox_rows(session: AsyncSession) -> dict[int, TelegramInboxStatus]:
    rows = (await session.scalars(select(TelegramUpdateInbox))).all()
    return {row.update_id: row.status for row in rows}


class SpyGateway:
    """Фейковый гейтвей: запись вызовов, никакого Telegram."""

    bot_id = BOT_ID

    def __init__(self, updates: list[TelegramUpdate] | None = None) -> None:
        self._updates = list(updates or [])
        self.events: list[tuple[str, object]] = []
        self.panels: list[TelegramUpdate] = []
        self.raise_on_answer = False

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        updates = self._updates
        self._updates = []
        return updates

    async def answer_callback(self, update: TelegramUpdate) -> None:
        if self.raise_on_answer:
            raise RuntimeError("answer failed")
        self.events.append(("answer_callback", update))

    async def send_panel(self, update: TelegramUpdate) -> None:
        self.panels.append(update)
        self.events.append(("send_panel", update))

    async def send_task_panel(
        self, update: TelegramUpdate, result: object, is_completion: bool
    ) -> None:
        self.events.append(("send_task_panel", (update, result, is_completion)))

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.events.append(("send_acknowledgement", (update, kind)))

    async def send_reminder_set(self, update: TelegramUpdate, when: datetime) -> None:
        self.events.append(("send_reminder_set", (update, when)))


class AcquiredPollerLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


class RecordingProcessor:
    def __init__(self, failing_update_ids: set[int] | None = None) -> None:
        self.processed: list[int] = []
        self._failing = failing_update_ids or set()

    async def process(self, update: TelegramUpdate) -> object:
        self.processed.append(update.update_id)
        if update.update_id in self._failing:
            raise RuntimeError("processing failed")
        return type("Result", (), {"kind": AcknowledgementKind.IGNORED})()


def build_step(
    engine: AsyncEngine,
    gateway: SpyGateway,
    processor: object,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    panel_followup_seconds: float = 0,
) -> TelegramInboxStep:
    return TelegramInboxStep(
        create_session_factory(engine),
        gateway,  # type: ignore[arg-type]
        processor,  # type: ignore[arg-type]
        max_attempts=max_attempts,
        retry_backoff=BACKOFF,
        panel_followup_seconds=panel_followup_seconds,
    )


def real_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused",
        schema_database_url="postgresql+asyncpg://unused-owner",
        telegram_bot_token=f"{BOT_ID}:token",
        invite_token_pepper=b"test-pepper",
        invite_token_pepper_key_id="test-key",
    )


def real_processor_pair(
    engine: AsyncEngine,
) -> tuple[object, async_sessionmaker[AsyncSession]]:
    session_factory = create_session_factory(engine)
    processor = build_update_processor(session_factory, real_settings(), None)
    return processor, session_factory


def canonical(events: list[tuple[str, object]]) -> list[tuple[str, object]]:
    """Вызовы гейтвея с обнулённым update_id: пути различаются только им."""

    def strip(value: object) -> object:
        if isinstance(value, TelegramUpdate):
            return replace(value, update_id=0)
        if isinstance(value, tuple):
            return tuple(strip(item) for item in value)
        return value

    return [(name, strip(payload)) for name, payload in events]


@pytest.mark.asyncio
async def test_step_processes_rows_strictly_in_update_id_order(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    for update_id in (203, 201, 202):
        await enqueue(engine, text_payload(update_id), NOW)
    gateway = SpyGateway()
    processor = RecordingProcessor()
    step = build_step(engine, gateway, processor)

    worked = await step.process_once(NOW + timedelta(minutes=1))

    assert worked is True
    assert processor.processed == [201, 202, 203]
    assert await inbox_rows(session) == {
        201: TelegramInboxStatus.DONE,
        202: TelegramInboxStatus.DONE,
        203: TelegramInboxStatus.DONE,
    }
    assert await step.process_once(NOW + timedelta(minutes=2)) is False


@pytest.mark.asyncio
async def test_failed_head_backs_off_then_fails_and_frees_the_tail(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await enqueue(engine, text_payload(211), NOW)
    await enqueue(engine, text_payload(212), NOW)
    gateway = SpyGateway()
    processor = RecordingProcessor(failing_update_ids={211})
    step = build_step(engine, gateway, processor)

    assert await step.process_once(NOW + timedelta(seconds=1)) is True
    # Первая попытка сорвалась: голова pending в бэкоффе, хвост НЕ трогается.
    assert processor.processed == [211]
    assert await inbox_rows(session) == {
        211: TelegramInboxStatus.PENDING,
        212: TelegramInboxStatus.PENDING,
    }

    # До созревания головы шаг не берёт ничего.
    assert await step.process_once(NOW + timedelta(seconds=30)) is False
    assert processor.processed == [211]

    # Голова созрела: попытка 2 (потолок) → failed; хвост освобождён и сделан.
    assert await step.process_once(NOW + BACKOFF + timedelta(seconds=2)) is True
    assert processor.processed == [211, 211, 212]
    assert await inbox_rows(session) == {
        211: TelegramInboxStatus.FAILED,
        212: TelegramInboxStatus.DONE,
    }


@pytest.mark.asyncio
async def test_unparseable_payload_fails_without_blocking_the_tail(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    poison = {"update_id": 221, "message": {"date": "мусор"}}
    await enqueue(engine, poison, NOW)
    await enqueue(engine, text_payload(222), NOW)
    gateway = SpyGateway()
    processor = RecordingProcessor()
    step = build_step(engine, gateway, processor, max_attempts=1)

    assert await step.process_once(NOW + timedelta(seconds=3)) is True

    assert processor.processed == [222]
    assert await inbox_rows(session) == {
        221: TelegramInboxStatus.FAILED,
        222: TelegramInboxStatus.DONE,
    }


@pytest.mark.asyncio
async def test_callback_is_answered_best_effort_before_processing(
    engine: AsyncEngine,
) -> None:
    await enqueue(engine, callback_payload(231), NOW)
    gateway = SpyGateway()

    class OrderRecordingProcessor:
        def __init__(self) -> None:
            self.answered_before_processing: list[bool] = []

        async def process(self, update: TelegramUpdate) -> object:
            self.answered_before_processing.append(
                ("answer_callback", update) in gateway.events
            )
            return type("Result", (), {"kind": AcknowledgementKind.IGNORED})()

    processor = OrderRecordingProcessor()
    step = build_step(engine, gateway, processor)

    await step.process_once(NOW + timedelta(seconds=4))

    assert processor.answered_before_processing == [True]


@pytest.mark.asyncio
async def test_failed_answer_callback_does_not_stop_processing(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await enqueue(engine, callback_payload(241), NOW)
    gateway = SpyGateway()
    gateway.raise_on_answer = True
    processor = RecordingProcessor()
    step = build_step(engine, gateway, processor)

    await step.process_once(NOW + timedelta(seconds=5))

    assert processor.processed == [241]
    assert await inbox_rows(session) == {241: TelegramInboxStatus.DONE}


@pytest.mark.asyncio
async def test_panel_followup_is_rescheduled_after_processing(
    engine: AsyncEngine,
) -> None:
    await enqueue(engine, text_payload(251), NOW)
    gateway = SpyGateway()

    class CapturedProcessor:
        async def process(self, update: TelegramUpdate) -> object:
            return type(
                "Result",
                (),
                {"kind": AcknowledgementKind.CAPTURED, "fresh": True},
            )()

    step = build_step(
        engine, gateway, CapturedProcessor(), panel_followup_seconds=TINY_DELAY
    )

    await step.process_once(NOW + timedelta(seconds=6))
    assert gateway.panels == []  # не сразу — только после задержки

    await asyncio.sleep(SETTLE)
    assert len(gateway.panels) == 1
    assert gateway.panels[0].update_id == 251

    await step.shutdown()
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []


@pytest.mark.asyncio
async def test_e2e_callback_reply_matches_poller_path_byte_for_byte(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_known_actor(schema_engine)

    # Поллер-путь: тот же callback-апдейт через LocalPoller.
    poller_processor, _ = real_processor_pair(engine)
    poller_update = normalize_aiogram_update(
        Update.model_validate(callback_payload(301)), BOT_ID
    )
    poller_gateway = SpyGateway(updates=[poller_update])
    poller = LocalPoller(
        poller_gateway,  # type: ignore[arg-type]
        poller_processor,  # type: ignore[arg-type]
        AcquiredPollerLock(),
    )
    await poller.run_once()

    # Inbox-путь: тот же payload (другой update_id) через webhook-очередь.
    await enqueue(engine, callback_payload(302), NOW)
    inbox_processor, _ = real_processor_pair(engine)
    inbox_gateway = SpyGateway()
    step = build_step(engine, inbox_gateway, inbox_processor)
    await step.process_once(NOW + timedelta(seconds=7))

    assert canonical(inbox_gateway.events) == canonical(poller_gateway.events)
    assert [name for name, _payload in inbox_gateway.events] == [
        "answer_callback",
        "send_task_panel",
    ]


@pytest.mark.asyncio
async def test_e2e_inbox_text_update_creates_capture_event_silently(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_known_actor(schema_engine)
    await enqueue(engine, text_payload(311, "заметка из webhook"), NOW)
    processor, _ = real_processor_pair(engine)
    gateway = SpyGateway()
    step = build_step(engine, gateway, processor)

    assert await step.process_once(NOW + timedelta(seconds=8)) is True

    assert await inbox_rows(session) == {311: TelegramInboxStatus.DONE}
    # Счёт под owner-ролью: RLS capture_events скрыла бы строки от app-сессии
    # без выставленного user_space-скоупа.
    async with schema_engine.connect() as connection:
        captured = await connection.scalar(
            select(func.count()).select_from(CaptureEventModel)
        )
    assert captured == 1
    receipt = (await session.scalars(select(TelegramUpdateReceipt))).one()
    assert (receipt.update_id, receipt.result_kind) == (311, "captured")
    assert gateway.events == []  # capture молчалив — как у поллера


@pytest.mark.asyncio
async def test_retry_after_crash_between_receipt_and_done_sends_no_duplicate(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Краш ПОСЛЕ коммита receipt'а, но ДО mark_done: строка снова pending.
    # Повторный прогон гасится receipt-идемпотентностью (fresh=False) —
    # ответ пользователю НЕ дублируется, строка честно доезжает до done.
    await seed_known_actor(schema_engine)
    await enqueue(engine, callback_payload(321), NOW)
    processor, _ = real_processor_pair(engine)
    first_attempt = normalize_aiogram_update(
        Update.model_validate(callback_payload(321)), BOT_ID
    )
    await processor.process(first_attempt)  # type: ignore[attr-defined]

    gateway = SpyGateway()
    step = build_step(engine, gateway, processor)
    assert await step.process_once(NOW + timedelta(seconds=9)) is True

    assert await inbox_rows(session) == {321: TelegramInboxStatus.DONE}
    # Ack кнопки — best-effort и повторяется (как у поллера при ретрае),
    # а вот панель задач ВТОРОЙ раз не уходит.
    assert [name for name, _payload in gateway.events] == ["answer_callback"]
    # done-строка больше не выдаётся.
    assert await step.process_once(NOW + timedelta(seconds=10)) is False

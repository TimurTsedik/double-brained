from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.types import Update
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.exact_search_in_transaction import ExactSearchInTransaction
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.bootstrap.voice_capture_in_transaction import (
    VoiceCaptureInTransaction,
)
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.capture.application.contracts import (
    CaptureVoiceCommand,
    TelegramVoiceMetadata,
)
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
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
    UpdateResult,
)
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    StoredUpdateReceipt,
)
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingRunModel,
    ProcessingStepModel,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from second_brain.slices.retrieval.application.contracts import SearchPanelResult
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)


@pytest_asyncio.fixture
async def voice_database(
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


class FixedClock:
    def now(self) -> datetime:
        return NOW


class KnownActorStore:
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        handler: object,
    ) -> StoredUpdateReceipt:
        result = await handler(self)
        assert isinstance(result, NewUpdateResult)
        return StoredUpdateReceipt(
            result.result_kind,
            result.trace_id,
            existing=False,
            span_id=result.span_id,
        )

    async def resolve_access_context(self, _telegram_user_id: int) -> AccessContext:
        return ACCESS


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(self, _telegram_user_id: int) -> None:
        return None


class RecordingVoicePort:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commands: list[CaptureVoiceCommand] = []

    async def capture(self, command: CaptureVoiceCommand, transaction: object) -> None:
        assert transaction is not None
        self.events.append("voice_capture")
        self.commands.append(command)


class RecordingSearchPort:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def set_awaiting(self, _command: object, _transaction: object) -> None:
        raise AssertionError("voice must not set search mode")

    async def cancel(self, access_context: AccessContext, _transaction: object) -> None:
        assert access_context == ACCESS
        self.events.append("search_cancel")

    async def consume_query(
        self, _command: object, _transaction: object
    ) -> SearchPanelResult | None:
        raise AssertionError("voice must not be interpreted as a search query")


def private_voice(update_id: int = 100) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=None,
        telegram_message_id=update_id + 1_000,
        voice=TelegramVoiceMetadata(
            file_id="private-file-id",
            file_unique_id="private-unique-id",
            duration_seconds=17,
            file_size=12_345,
            mime_type="audio/ogg",
        ),
    )


def test_aiogram_voice_is_normalized_without_exposing_file_ids() -> None:
    voice = SimpleNamespace(
        file_id="normalization-file-id",
        file_unique_id="normalization-unique-id",
        duration=23,
        file_size=98_765,
        mime_type="audio/ogg",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="private"),
        text=None,
        message_id=200,
        voice=voice,
    )
    update = SimpleNamespace(update_id=100, callback_query=None, message=message)
    gateway = AiogramGateway(cast(Bot, object()), bot_id=1)

    normalized = gateway._normalize(cast(Update, update))

    assert normalized.voice is not None
    assert normalized.voice.duration_seconds == 23
    assert normalized.voice.file_size == 98_765
    assert normalized.voice.mime_type == "audio/ogg"
    assert normalized.voice.file_id == "normalization-file-id"
    assert normalized.voice.file_unique_id == "normalization-unique-id"
    assert "normalization-file-id" not in repr(normalized)
    assert "normalization-unique-id" not in repr(normalized)
    assert "normalization-file-id" not in repr(normalized.voice)
    assert "normalization-unique-id" not in repr(normalized.voice)


@pytest.mark.asyncio
async def test_known_private_voice_cancels_search_then_is_queued() -> None:
    events: list[str] = []
    capture = RecordingVoicePort(events)
    processor = LocalUpdateProcessor(
        store=KnownActorStore(),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_voice_port=capture,
        exact_search_port=RecordingSearchPort(events),
    )

    result = await processor.process(private_voice())

    assert result.kind is AcknowledgementKind.VOICE_QUEUED
    assert events == ["search_cancel", "voice_capture"]
    assert len(capture.commands) == 1
    command = capture.commands[0]
    assert command.access_context == ACCESS
    assert command.telegram_message_id == 1_100
    assert command.voice.duration_seconds == 17
    assert command.trace_id == result.trace_id
    assert "private-file-id" not in repr(command)
    assert "private-unique-id" not in repr(command)


@pytest.mark.asyncio
async def test_group_unknown_and_callback_voice_are_ignored() -> None:
    events: list[str] = []
    capture = RecordingVoicePort(events)
    known_processor = LocalUpdateProcessor(
        store=KnownActorStore(),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_voice_port=capture,
        exact_search_port=RecordingSearchPort(events),
    )
    group = private_voice(101)
    group = TelegramUpdate(
        bot_id=group.bot_id,
        update_id=group.update_id,
        is_private=False,
        telegram_user_id=group.telegram_user_id,
        text=None,
        telegram_message_id=group.telegram_message_id,
        voice=group.voice,
    )
    callback = TelegramUpdate(
        bot_id=1,
        update_id=102,
        is_private=True,
        telegram_user_id=42,
        text=None,
        callback_query_id="callback-id",
        callback_data="unsupported",
        voice=private_voice().voice,
    )
    unknown_processor = LocalUpdateProcessor(
        store=UnknownActorStore(),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_voice_port=capture,
        exact_search_port=RecordingSearchPort(events),
    )

    results = [
        await known_processor.process(group),
        await known_processor.process(callback),
        await unknown_processor.process(private_voice(103)),
    ]

    assert [result.kind for result in results] == [AcknowledgementKind.IGNORED] * 3
    assert capture.commands == []
    assert events == []


def real_processor(
    engine: AsyncEngine,
    voice_capture: object | None = None,
) -> LocalUpdateProcessor:
    task_capture = TaskCaptureInTransaction()
    exact_search = ExactSearchInTransaction()
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_text_port=task_capture,
        task_mode_port=task_capture,
        task_panel_port=task_capture,
        exact_search_port=exact_search,
        capture_voice_port=(
            VoiceCaptureInTransaction() if voice_capture is None else voice_capture
        ),
    )


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


@pytest.mark.asyncio
async def test_fresh_voice_atomically_creates_source_attachment_run_steps_and_receipt(
    voice_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    update = private_voice(200)
    app = real_processor(engine)

    fresh = await app.process(update)
    duplicate = await app.process(update)

    assert fresh.kind is duplicate.kind is AcknowledgementKind.VOICE_QUEUED
    assert fresh.fresh is True
    assert duplicate.fresh is False
    assert fresh.trace_id == duplicate.trace_id
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TelegramAttachmentModel) == 1
    assert await count(schema_engine, ProcessingRunModel) == 1
    assert await count(schema_engine, ProcessingStepModel) == 3
    assert await count(schema_engine, TelegramUpdateReceipt) == 1

    async with create_session_factory(schema_engine)() as session:
        source = await session.scalar(select(CaptureEventModel))
        attachment = await session.scalar(select(TelegramAttachmentModel))
        run = await session.scalar(select(ProcessingRunModel))
        steps = tuple(
            await session.scalars(
                select(ProcessingStepModel).order_by(ProcessingStepModel.step_type)
            )
        )
        receipt = await session.scalar(select(TelegramUpdateReceipt))
    assert source is not None
    assert attachment is not None
    assert run is not None
    assert receipt is not None
    assert source.source_kind.value == "voice"
    assert source.raw_text is None
    assert attachment.capture_event_id == source.id
    assert attachment.telegram_file_id == "private-file-id"
    assert attachment.storage_key is None
    assert run.capture_event_id == source.id
    assert run.output_type is TranscriptionOutputType.NOTE
    assert {step.step_type for step in steps} == set(ProcessingStepType)
    assert {step.status for step in steps} == {ProcessingStepStatus.PENDING.value}
    assert receipt.result_kind == AcknowledgementKind.VOICE_QUEUED.value
    assert receipt.trace_id == source.trace_id == attachment.trace_id == run.trace_id


@pytest.mark.asyncio
async def test_voice_freezes_selected_type_and_resets_selection_to_note(
    voice_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    app = real_processor(engine)
    selection = TelegramUpdate(
        bot_id=1,
        update_id=210,
        is_private=True,
        telegram_user_id=42,
        text=None,
        callback_query_id="selection-callback",
        callback_data="capture:task",
    )

    await app.process(selection)
    result = await app.process(private_voice(211))

    assert result.kind is AcknowledgementKind.VOICE_QUEUED
    async with create_session_factory(schema_engine)() as session:
        run = await session.scalar(select(ProcessingRunModel))
        pending = await session.scalar(select(PendingCaptureSelectionModel))
    assert run is not None
    assert pending is not None
    assert run.output_type is TranscriptionOutputType.TASK
    assert pending.selection is PendingCaptureType.NOTE


@pytest.mark.asyncio
async def test_voice_cancels_pending_search_in_same_receipt_transaction(
    voice_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    app = real_processor(engine)
    search_button = TelegramUpdate(
        bot_id=1,
        update_id=220,
        is_private=True,
        telegram_user_id=42,
        text=None,
        callback_query_id="search-callback",
        callback_data="search:prompt",
    )
    await app.process(search_button)
    assert await count(schema_engine, PendingSearchModeModel) == 1

    result = await app.process(private_voice(221))

    assert result.kind is AcknowledgementKind.VOICE_QUEUED
    assert await count(schema_engine, PendingSearchModeModel) == 0


class FailingVoiceCapture(VoiceCaptureInTransaction):
    async def capture(
        self, command: CaptureVoiceCommand, transaction: object
    ) -> object:
        await super().capture(command, transaction)
        raise RuntimeError("voice transaction failed")


@pytest.mark.asyncio
async def test_voice_failure_rolls_back_every_row_and_retry_succeeds_once(
    voice_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    update = private_voice(230)

    with pytest.raises(RuntimeError, match="voice transaction failed"):
        await real_processor(engine, FailingVoiceCapture()).process(update)

    for model in (
        CaptureEventModel,
        TelegramAttachmentModel,
        ProcessingRunModel,
        ProcessingStepModel,
        TelegramUpdateReceipt,
    ):
        assert await count(schema_engine, model) == 0

    result = await real_processor(engine).process(update)

    assert result.kind is AcknowledgementKind.VOICE_QUEUED
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TelegramAttachmentModel) == 1
    assert await count(schema_engine, ProcessingRunModel) == 1
    assert await count(schema_engine, ProcessingStepModel) == 3
    assert await count(schema_engine, TelegramUpdateReceipt) == 1


class VoiceQueuedProcessor:
    def __init__(self, fresh: bool) -> None:
        self._fresh = fresh

    async def process(self, _update: TelegramUpdate) -> UpdateResult:
        return UpdateResult(
            kind=AcknowledgementKind.VOICE_QUEUED,
            trace_id="1" * 32,
            span_id="2" * 16,
            fresh=self._fresh,
        )


class VoiceQueuedGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.voice_statuses = 0
        self.generic_statuses = 0

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        return [self._update]

    async def send_voice_queued(self, _update: TelegramUpdate) -> None:
        self.voice_statuses += 1

    async def send_acknowledgement(
        self, _update: TelegramUpdate, _kind: AcknowledgementKind
    ) -> None:
        self.generic_statuses += 1


class AcquiredLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(("fresh", "expected_statuses"), [(True, 1), (False, 0)])
async def test_voice_queued_status_is_sent_only_for_fresh_receipt(
    fresh: bool, expected_statuses: int
) -> None:
    gateway = VoiceQueuedGateway(private_voice(240))
    poller = LocalPoller(gateway, VoiceQueuedProcessor(fresh), AcquiredLock())

    await poller.run_once()

    assert gateway.voice_statuses == expected_statuses
    assert gateway.generic_statuses == 0
    assert poller.offset == 241


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_compact_voice_queued_status() -> None:
    bot = RecordingBot()
    gateway = AiogramGateway(cast(Bot, bot), bot_id=1)

    await gateway.send_voice_queued(private_voice(241))

    assert bot.messages == [(42, "🎙️ Голос сохранён. Расшифровываю…")]

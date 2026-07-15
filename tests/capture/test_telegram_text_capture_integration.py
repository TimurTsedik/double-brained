from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.capture_in_transaction import CaptureInTransaction
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
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
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    StoredUpdateReceipt,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_capture_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
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

    async def read_user_space_language(
        self, _access_context: AccessContext
    ) -> str | None:
        return "ru"


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(self, _telegram_user_id: int) -> None:
        return None


class RecordingCapturePort:
    def __init__(self) -> None:
        self.commands: list[CaptureTextCommand] = []

    async def capture(self, command: CaptureTextCommand, transaction: object) -> None:
        assert transaction is not None
        self.commands.append(command)


class DelegatingCapturePort:
    def __init__(self, delegate: CaptureInTransaction) -> None:
        self._delegate = delegate
        self.calls = 0

    async def capture(self, command: CaptureTextCommand, transaction: object) -> None:
        self.calls += 1
        await self._delegate.capture(command, transaction)


class FailingAfterCapturePort:
    def __init__(self) -> None:
        self._delegate = CaptureInTransaction()

    async def capture(self, command: CaptureTextCommand, transaction: object) -> None:
        await self._delegate.capture(command, transaction)
        raise RuntimeError("capture transaction failed")


class CapturedProcessor:
    async def process(self, _update: TelegramUpdate):
        return type("Result", (), {"kind": AcknowledgementKind.CAPTURED})()


class CapturedGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.sent_acknowledgements: list[AcknowledgementKind] = []

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        return [self._update]

    async def send_acknowledgement(
        self, _update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.sent_acknowledgements.append(kind)


class AcquiredPollerLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


@pytest.mark.asyncio
async def test_known_private_plain_text_creates_a_captured_event_request() -> None:
    capture_port = RecordingCapturePort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        capture_port,
    )
    update = TelegramUpdate(
        bot_id=1,
        update_id=100,
        is_private=True,
        telegram_user_id=42,
        text="remember this",
        telegram_message_id=200,
    )

    result = await processor.process(update)

    assert result.kind is AcknowledgementKind.CAPTURED
    assert len(capture_port.commands) == 1
    command = capture_port.commands[0]
    assert command.access_context == ACCESS
    assert command.raw_text == "remember this"
    assert command.telegram_message_id == 200
    assert command.received_at == NOW
    assert command.trace_id == result.trace_id


async def seed_known_actor(schema_engine: AsyncEngine) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="member",
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
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def private_text(update_id: int, text: str = "remember this") -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=text,
        telegram_message_id=update_id + 1000,
    )


def real_processor(
    engine: AsyncEngine, capture_port: object | None = None
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        CaptureInTransaction() if capture_port is None else capture_port,
    )


@pytest.mark.asyncio
async def test_known_private_plain_text_creates_one_captured_event(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    await seed_known_actor(schema_engine)
    processor = real_processor(engine)
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))

    result = await processor.process(private_text(101))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert await capture_repository.count(ACCESS) == 1
    receipt = await session.scalar(select(TelegramUpdateReceipt))
    assert receipt is not None
    assert receipt.result_kind == AcknowledgementKind.CAPTURED.value
    assert receipt.trace_id == result.trace_id


@pytest.mark.asyncio
async def test_duplicate_text_update_does_not_invoke_capture_twice(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_known_actor(schema_engine)
    capture_port = DelegatingCapturePort(CaptureInTransaction())
    processor = real_processor(engine, capture_port)
    update = private_text(102)

    first = await processor.process(update)
    second = await processor.process(update)

    assert first.kind is AcknowledgementKind.CAPTURED
    assert second.kind is AcknowledgementKind.CAPTURED
    assert first.trace_id == second.trace_id
    assert capture_port.calls == 1


@pytest.mark.asyncio
async def test_unsupported_input_does_not_invoke_capture() -> None:
    capture_port = RecordingCapturePort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        capture_port,
    )
    updates = [
        TelegramUpdate(1, 103, False, 42, "group text", 1103),
        TelegramUpdate(1, 104, True, None, "missing actor", 1104),
        TelegramUpdate(1, 105, True, 42, "", 1105),
        TelegramUpdate(1, 106, True, 42, "/not-a-capture", 1106),
        TelegramUpdate(1, 107, True, 42, "/start a-token", 1107),
        TelegramUpdate(1, 108, True, 42, "  /not-a-capture", 1108),
        TelegramUpdate(1, 109, True, 42, "\n/start a-token", 1109),
    ]

    results = [await processor.process(update) for update in updates]
    unknown_result = await LocalUpdateProcessor(
        UnknownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        capture_port,
    ).process(TelegramUpdate(1, 108, True, 404, "unknown actor", 1108))

    # Известный актёр, повторно открывший invite («/start a-token»), получает
    # welcome-back (KNOWN_USER_STARTED), а не молчание; «\n/start …» — обычная
    # команда → IGNORED. Ни один путь не заводит capture.
    assert [result.kind for result in results] == [
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.KNOWN_USER_STARTED,
        AcknowledgementKind.IGNORED,
        AcknowledgementKind.IGNORED,
    ]
    assert unknown_result.kind is AcknowledgementKind.IGNORED
    assert capture_port.commands == []


def test_capture_command_repr_omits_raw_text() -> None:
    command = CaptureTextCommand(
        access_context=ACCESS,
        bot_id=1,
        telegram_update_id=110,
        telegram_message_id=1110,
        raw_text="capture-command-secret",
        received_at=NOW,
        trace_id="1" * 32,
    )

    assert "capture-command-secret" not in repr(command)


@pytest.mark.asyncio
async def test_capture_failure_rolls_back_event_and_receipt_then_retry_captures_once(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    await seed_known_actor(schema_engine)
    update = private_text(107)
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="capture transaction failed"):
        await real_processor(engine, FailingAfterCapturePort()).process(update)

    assert await capture_repository.count(ACCESS) == 0
    assert (
        await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt))
        == 0
    )

    result = await real_processor(engine).process(update)

    assert result.kind is AcknowledgementKind.CAPTURED
    assert await capture_repository.count(ACCESS) == 1
    assert (
        await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt))
        == 1
    )


@pytest.mark.asyncio
async def test_captured_result_sends_no_reply_and_advances_offset() -> None:
    update = private_text(108)
    gateway = CapturedGateway(update)
    poller = LocalPoller(gateway, CapturedProcessor(), AcquiredPollerLock())

    await poller.run_once()

    assert gateway.sent_acknowledgements == []
    assert poller.offset == update.update_id + 1

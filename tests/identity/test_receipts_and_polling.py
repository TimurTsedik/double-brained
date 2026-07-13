from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.shared.trace import TraceContext
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramUpdateReceipt,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    reset_prototype_schema,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.poller import (
    LocalPoller,
    PollerAlreadyRunning,
    WebhookConfigured,
)
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.identity.ports.repositories import (
    EnrollmentAttemptReservation,
    EnrollmentOutcome,
    StoredUpdateReceipt,
)

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
PEPPER = b"task5-pepper"


class FakeStore:
    def __init__(self) -> None:
        self.receipts: dict[tuple[int, int], tuple[str, str]] = {}
        self.known_actors: set[int] = set()
        self.attempts: list[tuple[bytes, datetime]] = []

    async def process_once(
        self,
        bot_id: int,
        update_id: int,
        occurred_at: datetime,
        handler: object,
    ) -> StoredUpdateReceipt:
        receipt = self.receipts.get((bot_id, update_id))
        if receipt is not None:
            return StoredUpdateReceipt(*receipt, existing=True)
        result = await handler(self)
        self.receipts[(bot_id, update_id)] = (result.result_kind, result.trace_id)
        return StoredUpdateReceipt(
            result.result_kind,
            result.trace_id,
            existing=False,
            span_id=result.span_id,
        )

    async def resolve_access_context(self, telegram_user_id: int):
        if telegram_user_id not in self.known_actors:
            return None
        return object()

    async def reserve_enrollment_attempt(
        self,
        bot_id: int,
        actor_digest: bytes,
        pepper_key_id: str,
        trace_id: str,
        created_at: datetime,
    ) -> EnrollmentAttemptReservation:
        admitted = len(self.attempts) < 5
        self.attempts.append((actor_digest, created_at))
        return EnrollmentAttemptReservation(uuid4(), admitted)

    async def finish_enrollment_attempt(
        self, attempt_id: UUID, result_code: str
    ) -> None:
        return None

    async def enroll_telegram_user(
        self,
        token_hash: bytes,
        pepper_key_id: str,
        telegram_user_id: int,
        now: datetime,
    ) -> EnrollmentOutcome:
        return EnrollmentOutcome.REJECTED


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeGateway:
    def __init__(
        self, updates: list[TelegramUpdate], webhook_url: str | None = None
    ) -> None:
        self.updates = updates
        self.webhook_url = webhook_url
        self.allowed_updates: list[str] | None = None
        self.replies: list[AcknowledgementKind] = []
        self.raise_on_reply = False

    async def configured_webhook_url(self) -> str | None:
        return self.webhook_url

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        self.allowed_updates = allowed_updates
        return self.updates

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        if self.raise_on_reply:
            raise RuntimeError("reply failed")
        self.replies.append(kind)


class FakePollerLock:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired

    async def acquire(self, bot_id: int) -> bool:
        return self.acquired


class FakeProcessor:
    async def process(self, update: TelegramUpdate):
        return type("Result", (), {"kind": AcknowledgementKind.IGNORED})()


class FailsOnceProcessor:
    def __init__(self) -> None:
        self.calls = 0

    async def process(self, update: TelegramUpdate):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("durable transaction failed")
        return type("Result", (), {"kind": AcknowledgementKind.IGNORED})()


@pytest_asyncio.fixture(autouse=True)
async def reset_task5_schema(engine: AsyncEngine) -> None:
    await reset_prototype_schema(engine, confirm=True)


def private_start(update_id: int, token: str | None = "token") -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text="/start" if token is None else f"/start {token}",
    )


def test_telegram_update_is_immutable() -> None:
    update = private_start(1)

    with pytest.raises(FrozenInstanceError):
        update.update_id = 2


@pytest.mark.asyncio
async def test_duplicate_receipt_reuses_trace_and_skips_token_parse() -> None:
    store = FakeStore()
    processor = LocalUpdateProcessor(store, FixedClock(), PEPPER, "key-1")
    update = private_start(2, token="secret-start-token")

    first = await processor.process(update)
    second = await processor.process(update)

    assert first.trace_id == second.trace_id
    assert first.span_id != second.span_id
    assert len(store.attempts) == 1


@pytest.mark.asyncio
async def test_existing_receipt_does_not_create_a_root_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    store.receipts[(1, 8)] = (AcknowledgementKind.IGNORED, "1" * 32)
    processor = LocalUpdateProcessor(store, FixedClock(), PEPPER, "key-1")

    def root_trace_must_not_be_created() -> TraceContext:
        raise AssertionError("duplicate processing must reuse the stored trace")

    monkeypatch.setattr(
        TraceContext, "new_root", staticmethod(root_trace_must_not_be_created)
    )

    result = await processor.process(private_start(8))

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.trace_id == "1" * 32


@pytest.mark.asyncio
async def test_processor_rate_limits_unknown_actor_after_five_attempts() -> None:
    store = FakeStore()
    processor = LocalUpdateProcessor(store, FixedClock(), PEPPER, "key-1")

    results = [
        await processor.process(private_start(update_id)) for update_id in range(1, 7)
    ]

    assert [result.kind for result in results] == [
        AcknowledgementKind.ENROLLMENT_REJECTED,
        AcknowledgementKind.ENROLLMENT_REJECTED,
        AcknowledgementKind.ENROLLMENT_REJECTED,
        AcknowledgementKind.ENROLLMENT_REJECTED,
        AcknowledgementKind.ENROLLMENT_REJECTED,
        AcknowledgementKind.ENROLLMENT_REJECTED,
    ]
    assert len(store.attempts) == 6


@pytest.mark.asyncio
async def test_processor_ignores_non_private_updates_and_recognizes_known_start() -> (
    None
):
    store = FakeStore()
    store.known_actors.add(42)
    processor = LocalUpdateProcessor(store, FixedClock(), PEPPER, "key-1")
    ignored = TelegramUpdate(1, 3, False, 42, "/start token")
    known = private_start(4, token=None)

    assert (await processor.process(ignored)).kind is AcknowledgementKind.IGNORED
    assert (
        await processor.process(known)
    ).kind is AcknowledgementKind.KNOWN_USER_STARTED


@pytest.mark.asyncio
async def test_postgres_receipt_reuses_existing_receipt_without_raw_token(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    repository = PostgresUpdateRepository(create_session_factory(engine))
    first = await repository.process_once(
        1,
        5,
        NOW,
        lambda _transaction: _new_update_result(AcknowledgementKind.IGNORED),
    )
    second = await repository.process_once(
        1,
        5,
        NOW,
        lambda _transaction: _new_update_result(
            AcknowledgementKind.ENROLLMENT_REJECTED
        ),
    )
    receipt = await session.scalar(select(TelegramUpdateReceipt))

    assert first.result_kind == second.result_kind
    assert first.trace_id == second.trace_id
    assert first.existing is False
    assert second.existing is True
    assert receipt is not None
    assert receipt.result_kind == AcknowledgementKind.IGNORED.value
    assert "secret-start-token" not in repr(receipt)


async def _new_update_result(kind: AcknowledgementKind):
    context = TraceContext.new_root()
    from second_brain.slices.identity.ports.repositories import NewUpdateResult

    return NewUpdateResult(kind.value, context.trace_id, context.span_id)


@pytest.mark.asyncio
async def test_postgres_receipt_constraints_reject_invalid_acknowledgement_or_trace_id(
    session: AsyncSession,
) -> None:
    session.add(
        TelegramUpdateReceipt(
            bot_id=31,
            update_id=1,
            result_kind="untrusted_kind",
            trace_id="1" * 32,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()
    session.add(
        TelegramUpdateReceipt(
            bot_id=31,
            update_id=2,
            result_kind=AcknowledgementKind.IGNORED.value,
            trace_id="g" * 32,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()
    session.add(
        TelegramUpdateReceipt(
            bot_id=31,
            update_id=3,
            result_kind=AcknowledgementKind.IGNORED.value,
            trace_id="0" * 32,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_poller_refuses_webhook_and_second_poller() -> None:
    update = private_start(6)
    with pytest.raises(WebhookConfigured):
        await LocalPoller(
            FakeGateway([update], webhook_url="https://example.test/hook"),
            FakeProcessor(),
            FakePollerLock(True),
        ).run_once()

    with pytest.raises(PollerAlreadyRunning):
        await LocalPoller(
            FakeGateway([update]), FakeProcessor(), FakePollerLock(False)
        ).run_once()


@pytest.mark.asyncio
async def test_poller_advances_offset_before_best_effort_reply() -> None:
    gateway = FakeGateway([private_start(7)])
    gateway.raise_on_reply = True
    poller = LocalPoller(gateway, FakeProcessor(), FakePollerLock(True))

    await poller.run_once()

    assert gateway.allowed_updates == ["message"]
    assert poller.offset == 8


@pytest.mark.asyncio
async def test_poller_retries_failed_processor_without_advancing_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway([private_start(9)])
    processor = FailsOnceProcessor()
    sleeps: list[float] = []

    async def record_backoff(delay: float) -> None:
        sleeps.append(delay)

    poller = LocalPoller(gateway, processor, FakePollerLock(True))
    monkeypatch.setattr(poller, "_sleep", record_backoff)

    await poller.run_once()

    assert processor.calls == 2
    assert sleeps == [1.0]
    assert poller.offset == 10

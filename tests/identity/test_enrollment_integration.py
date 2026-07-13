import asyncio
import base64
import hmac
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.shared.clock import Clock
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    EnrollmentInvite,
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresEnrollmentRepository,
)
from second_brain.slices.identity.adapters.persistence.schema import (
    reset_prototype_schema,
)
from second_brain.slices.identity.application.enrollment import (
    CreateEnrollmentInvite,
    EnrollTelegramUser,
)
from second_brain.slices.identity.ports.repositories import (
    BootstrapInviteUnavailable,
    EnrollmentOutcome,
)

PEPPER = b"test-invite-pepper"
PEPPER_KEY_ID = "test-v1"
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


DEFAULT_CLOCK = FixedClock(NOW)


@pytest_asyncio.fixture(autouse=True)
async def reset_enrollment_schema(engine: AsyncEngine) -> None:
    await reset_prototype_schema(engine, confirm=True)


def repository(engine: AsyncEngine) -> PostgresEnrollmentRepository:
    return PostgresEnrollmentRepository(create_session_factory(engine))


def create_invite_use_case(
    engine: AsyncEngine,
    clock: Clock = DEFAULT_CLOCK,
    pepper_key_id: str = PEPPER_KEY_ID,
) -> CreateEnrollmentInvite:
    return CreateEnrollmentInvite(
        repository=repository(engine),
        clock=clock,
        pepper=PEPPER,
        pepper_key_id=pepper_key_id,
    )


def enroll_use_case(
    engine: AsyncEngine,
    clock: Clock = DEFAULT_CLOCK,
    pepper_key_id: str = PEPPER_KEY_ID,
) -> EnrollTelegramUser:
    return EnrollTelegramUser(
        repository=repository(engine),
        clock=clock,
        pepper=PEPPER,
        pepper_key_id=pepper_key_id,
    )


async def record_count(session: AsyncSession, model: object) -> int:
    count = await session.scalar(select(func.count()).select_from(model))
    return int(count)


@pytest.mark.asyncio
async def test_bootstrap_invite_returns_unpadded_32_byte_token_and_persists_hmac_only(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    created = await create_invite_use_case(engine).execute()

    decoded = base64.urlsafe_b64decode(created.token + "=" * (-len(created.token) % 4))
    invite = await session.scalar(select(EnrollmentInvite))

    assert len(decoded) == 32
    assert "=" not in created.token
    assert created.expires_at == NOW + timedelta(hours=24)
    assert invite is not None
    assert (
        invite.token_hash == hmac.new(PEPPER, created.token.encode(), sha256).digest()
    )
    assert created.token.encode() not in invite.token_hash
    assert created.token not in repr(invite)
    assert created.token not in repr(created)
    assert invite.created_by_actor == "bootstrap_cli"
    assert invite.pepper_key_id == PEPPER_KEY_ID
    assert invite.status == "pending"


@pytest.mark.asyncio
async def test_bootstrap_invite_is_singleton_under_concurrent_creation(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    creator = create_invite_use_case(engine)

    results = await asyncio.gather(
        creator.execute(),
        creator.execute(),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert (
        sum(isinstance(result, BootstrapInviteUnavailable) for result in results) == 1
    )
    assert await record_count(session, EnrollmentInvite) == 1


@pytest.mark.asyncio
async def test_bootstrap_invite_refuses_a_second_invite_after_enrollment(
    engine: AsyncEngine,
) -> None:
    created = await create_invite_use_case(engine).execute()
    outcome = await enroll_use_case(engine).execute(
        token=created.token,
        telegram_user_id=101,
    )

    with pytest.raises(BootstrapInviteUnavailable):
        await create_invite_use_case(engine).execute()

    assert outcome is EnrollmentOutcome.ENROLLED


@pytest.mark.asyncio
async def test_enrollment_rejects_an_expired_invite(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    expired = await create_invite_use_case(engine).execute()
    expired_outcome = await enroll_use_case(
        engine,
        clock=FixedClock(NOW + timedelta(hours=24)),
    ).execute(token=expired.token, telegram_user_id=102)
    expired_invite = await session.scalar(select(EnrollmentInvite))

    assert expired_outcome is EnrollmentOutcome.REJECTED
    assert expired_invite is not None
    assert expired_invite.status == "expired"
    assert await record_count(session, User) == 0


@pytest.mark.asyncio
async def test_enrollment_rejects_an_invite_with_a_stale_pepper_key_id(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    stale = await create_invite_use_case(engine, pepper_key_id="old-key").execute()
    stale_outcome = await enroll_use_case(engine, pepper_key_id="new-key").execute(
        token=stale.token,
        telegram_user_id=103,
    )

    assert stale_outcome is EnrollmentOutcome.REJECTED
    assert await record_count(session, User) == 0


@pytest.mark.asyncio
async def test_enrollment_creates_admin_space_identity_and_consumes_invite_atomically(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    created = await create_invite_use_case(engine).execute()

    outcome = await enroll_use_case(engine).execute(
        token=created.token,
        telegram_user_id=104,
    )

    invite = await session.scalar(select(EnrollmentInvite))
    user = await session.scalar(select(User))
    user_space = await session.scalar(select(UserSpace))
    identity = await session.scalar(select(TelegramIdentity))

    assert outcome is EnrollmentOutcome.ENROLLED
    assert invite is not None and invite.status == "consumed"
    assert user is not None and user.role == "admin"
    assert user_space is not None and user_space.timezone == "Asia/Jerusalem"
    assert identity is not None and identity.telegram_user_id == 104
    assert user_space.owner_user_id == user.id
    assert identity.user_id == user.id
    assert invite.consumed_user_id == user.id
    assert invite.consumed_at == NOW


@pytest.mark.asyncio
async def test_concurrent_redemption_creates_only_one_administrator(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    created = await create_invite_use_case(engine).execute()
    enrollment = enroll_use_case(engine)

    outcomes = await asyncio.gather(
        enrollment.execute(token=created.token, telegram_user_id=105),
        enrollment.execute(token=created.token, telegram_user_id=106),
    )

    assert outcomes.count(EnrollmentOutcome.ENROLLED) == 1
    assert outcomes.count(EnrollmentOutcome.REJECTED) == 1
    assert await record_count(session, User) == 1
    assert await record_count(session, UserSpace) == 1
    assert await record_count(session, TelegramIdentity) == 1
    assert await record_count(session, EnrollmentInvite) == 1


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_consumption_and_identity_records(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    created = await create_invite_use_case(engine).execute()

    with pytest.raises(DBAPIError) as error:
        await enroll_use_case(engine).execute(
            token=created.token,
            telegram_user_id=2**63,
        )

    invite = await session.scalar(select(EnrollmentInvite))

    assert created.token not in str(error.value)
    assert invite is not None and invite.status == "pending"
    assert invite.consumed_at is None
    assert await record_count(session, User) == 0
    assert await record_count(session, UserSpace) == 0
    assert await record_count(session, TelegramIdentity) == 0

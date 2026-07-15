"""Много-пользовательский enrollment по member-invite (repository-уровень).

Реальный PostgreSQL: дедуп телеграма, выбор invite по token_hash+pepper_key_id под
row-lock, гейт единственного admin, ALREADY_ENROLLED и отсутствие дублей/orphan —
всё под BOOTSTRAP_LOCK в ОДНОЙ транзакции, фейк это не докажет.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from hmac import digest
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
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
from second_brain.slices.identity.application.enrollment import EnrollTelegramUser
from second_brain.slices.identity.ports.repositories import EnrollmentOutcome
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PEPPER = b"member-invite-pepper"
PEPPER_KEY_ID = "member-key"


class FixedClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


@pytest_asyncio.fixture(autouse=True)
async def reset_member_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


def enroll_use_case(
    engine: AsyncEngine, pepper_key_id: str = PEPPER_KEY_ID
) -> EnrollTelegramUser:
    return EnrollTelegramUser(
        repository=PostgresEnrollmentRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=PEPPER,
        pepper_key_id=pepper_key_id,
    )


async def seed_invite(
    schema_engine: AsyncEngine,
    *,
    token: str,
    role: str = "member",
    created_by_actor: str = "admin_bot",
    pepper_key_id: str = PEPPER_KEY_ID,
    status: str = "pending",
    expires_at: datetime = NOW + timedelta(hours=24),
) -> None:
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            session.add(
                EnrollmentInvite(
                    id=uuid4(),
                    token_hash=digest(PEPPER, token.encode(), "sha256"),
                    pepper_key_id=pepper_key_id,
                    role=role,
                    status=status,
                    created_by_actor=created_by_actor,
                    created_at=NOW,
                    expires_at=expires_at,
                )
            )


async def seed_active_admin(schema_engine: AsyncEngine, telegram_user_id: int) -> None:
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            user = User(id=uuid4(), role="admin", created_at=NOW, updated_at=NOW)
            session.add(user)
            await session.flush()
            session.add_all(
                [
                    UserSpace(
                        id=uuid4(),
                        owner_user_id=user.id,
                        timezone="Asia/Jerusalem",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    TelegramIdentity(
                        id=uuid4(),
                        telegram_user_id=telegram_user_id,
                        user_id=user.id,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )


async def count(session: AsyncSession, model: object) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.asyncio
async def test_member_invite_enrolls_a_member_with_own_space(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_invite(schema_engine, token="member-token")

    outcome = await enroll_use_case(engine).execute(
        token="member-token", telegram_user_id=5001
    )

    user = await session.scalar(select(User))
    space = await session.scalar(select(UserSpace))
    identity = await session.scalar(select(TelegramIdentity))
    invite = await session.scalar(select(EnrollmentInvite))
    assert outcome is EnrollmentOutcome.ENROLLED
    assert user is not None and user.role == "member"
    assert space is not None and space.owner_user_id == user.id
    assert identity is not None and identity.telegram_user_id == 5001
    assert invite is not None and invite.status == "consumed"


@pytest.mark.asyncio
async def test_multiple_pending_member_invites_enroll_their_own_person(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_invite(schema_engine, token="token-a")
    await seed_invite(schema_engine, token="token-b")

    first = await enroll_use_case(engine).execute(
        token="token-a", telegram_user_id=6001
    )
    second = await enroll_use_case(engine).execute(
        token="token-b", telegram_user_id=6002
    )

    telegram_ids = set(
        (await session.scalars(select(TelegramIdentity.telegram_user_id))).all()
    )
    assert first is EnrollmentOutcome.ENROLLED
    assert second is EnrollmentOutcome.ENROLLED
    assert telegram_ids == {6001, 6002}
    assert await count(session, User) == 2


@pytest.mark.asyncio
async def test_already_enrolled_telegram_user_is_graceful(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_invite(schema_engine, token="first-token")
    await enroll_use_case(engine).execute(token="first-token", telegram_user_id=7001)
    await seed_invite(schema_engine, token="second-token")

    outcome = await enroll_use_case(engine).execute(
        token="second-token", telegram_user_id=7001
    )

    second_invite = await session.scalar(
        select(EnrollmentInvite).where(
            EnrollmentInvite.token_hash == digest(PEPPER, b"second-token", "sha256")
        )
    )
    assert outcome is EnrollmentOutcome.ALREADY_ENROLLED
    assert await count(session, User) == 1
    assert await count(session, TelegramIdentity) == 1
    assert second_invite is not None and second_invite.status == "pending"


@pytest.mark.asyncio
async def test_two_member_invites_for_one_telegram_yield_one_enrollment(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_invite(schema_engine, token="race-a")
    await seed_invite(schema_engine, token="race-b")
    enroll = enroll_use_case(engine)

    outcomes = await asyncio.gather(
        enroll.execute(token="race-a", telegram_user_id=8001),
        enroll.execute(token="race-b", telegram_user_id=8001),
    )

    assert outcomes.count(EnrollmentOutcome.ENROLLED) == 1
    assert outcomes.count(EnrollmentOutcome.ALREADY_ENROLLED) == 1
    assert await count(session, User) == 1
    assert await count(session, TelegramIdentity) == 1
    # Ровно один invite потреблён, второй остался pending (не сожжён).
    consumed = await session.scalar(
        select(func.count())
        .select_from(EnrollmentInvite)
        .where(EnrollmentInvite.status == "consumed")
    )
    assert consumed == 1


@pytest.mark.asyncio
async def test_admin_invite_rejected_when_active_admin_exists(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_active_admin(schema_engine, telegram_user_id=9000)
    await seed_invite(
        schema_engine,
        token="second-admin",
        role="admin",
        created_by_actor="bootstrap_cli",
    )

    outcome = await enroll_use_case(engine).execute(
        token="second-admin", telegram_user_id=9001
    )

    invite = await session.scalar(
        select(EnrollmentInvite).where(EnrollmentInvite.role == "admin")
    )
    admins = await session.scalar(
        select(func.count()).select_from(User).where(User.role == "admin")
    )
    assert outcome is EnrollmentOutcome.REJECTED
    assert admins == 1
    assert invite is not None and invite.status == "pending"


@pytest.mark.asyncio
async def test_stale_pepper_key_with_matching_token_hash_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_invite(schema_engine, token="stale-token", pepper_key_id="old-key")

    outcome = await enroll_use_case(engine, pepper_key_id="new-key").execute(
        token="stale-token", telegram_user_id=9101
    )

    invite = await session.scalar(select(EnrollmentInvite))
    assert outcome is EnrollmentOutcome.REJECTED
    assert await count(session, User) == 0
    assert invite is not None and invite.status == "pending"


@pytest.mark.asyncio
async def test_expired_lookup_does_not_mutate_another_pending(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_invite(
        schema_engine, token="expired-token", expires_at=NOW - timedelta(minutes=1)
    )
    await seed_invite(schema_engine, token="valid-token")

    expired = await enroll_use_case(engine).execute(
        token="expired-token", telegram_user_id=9201
    )
    valid = await enroll_use_case(engine).execute(
        token="valid-token", telegram_user_id=9202
    )

    other = await session.scalar(
        select(EnrollmentInvite).where(
            EnrollmentInvite.token_hash == digest(PEPPER, b"valid-token", "sha256")
        )
    )
    assert expired is EnrollmentOutcome.REJECTED
    assert valid is EnrollmentOutcome.ENROLLED
    # Валидный pending не был затронут отбраковкой истёкшего; затем сам подключил.
    assert other is not None and other.status == "consumed"

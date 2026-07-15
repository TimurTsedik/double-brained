from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hmac import digest
from secrets import token_urlsafe
from uuid import UUID, uuid4

from second_brain.shared.clock import Clock
from second_brain.slices.identity.ports.repositories import (
    EnrollmentOutcome,
    EnrollmentRepository,
    NewBootstrapInvite,
)


@dataclass(frozen=True)
class BootstrapInvite:
    id: UUID
    token: str = field(repr=False)
    expires_at: datetime


class CreateEnrollmentInvite:
    def __init__(
        self,
        repository: EnrollmentRepository,
        clock: Clock,
        pepper: bytes,
        pepper_key_id: str,
        role: str = "admin",
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._pepper = pepper
        self._pepper_key_id = pepper_key_id
        self._role = role

    async def execute(self) -> BootstrapInvite:
        token = token_urlsafe(32)
        created_at = self._clock.now()
        expires_at = created_at + timedelta(hours=24)
        invite = NewBootstrapInvite(
            id=uuid4(),
            token_hash=digest(self._pepper, token.encode(), "sha256"),
            pepper_key_id=self._pepper_key_id,
            created_at=created_at,
            expires_at=expires_at,
            role=self._role,
        )
        await self._repository.store_bootstrap_invite(invite)
        return BootstrapInvite(id=invite.id, token=token, expires_at=expires_at)


class EnrollTelegramUser:
    def __init__(
        self,
        repository: EnrollmentRepository,
        clock: Clock,
        pepper: bytes,
        pepper_key_id: str,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._pepper = pepper
        self._pepper_key_id = pepper_key_id

    async def execute(self, token: str, telegram_user_id: int) -> EnrollmentOutcome:
        return await self._repository.enroll_telegram_user(
            token_hash=digest(self._pepper, token.encode(), "sha256"),
            pepper_key_id=self._pepper_key_id,
            telegram_user_id=telegram_user_id,
            now=self._clock.now(),
        )

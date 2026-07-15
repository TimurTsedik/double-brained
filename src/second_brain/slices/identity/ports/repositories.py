from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
)
from second_brain.slices.identity.application.contracts import (
    UpdateTransaction as UpdateTransactionContract,
)


class BootstrapInviteUnavailable(RuntimeError):
    pass


class EnrollmentOutcome(StrEnum):
    ENROLLED = "enrolled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class NewBootstrapInvite:
    id: UUID
    token_hash: bytes
    pepper_key_id: str
    created_at: datetime
    expires_at: datetime


class EnrollmentRepository(Protocol):
    async def store_bootstrap_invite(self, invite: NewBootstrapInvite) -> None: ...

    async def enroll_telegram_user(
        self,
        token_hash: bytes,
        pepper_key_id: str,
        telegram_user_id: int,
        now: datetime,
    ) -> EnrollmentOutcome: ...


@dataclass(frozen=True)
class StoredUpdateReceipt:
    result_kind: str
    trace_id: str
    existing: bool
    span_id: str | None = None


@dataclass(frozen=True)
class NewUpdateResult:
    result_kind: str
    trace_id: str
    span_id: str


@dataclass(frozen=True)
class EnrollmentAttemptReservation:
    id: UUID
    admitted: bool


class TelegramAccessContextResolver(Protocol):
    async def resolve_access_context(
        self, telegram_user_id: int
    ) -> AccessContext | None: ...


class UpdateTransaction(
    UpdateTransactionContract, TelegramAccessContextResolver, Protocol
):
    async def reserve_enrollment_attempt(
        self,
        bot_id: int,
        actor_digest: bytes,
        pepper_key_id: str,
        trace_id: str,
        created_at: datetime,
    ) -> EnrollmentAttemptReservation: ...

    async def finish_enrollment_attempt(
        self, attempt_id: UUID, result_code: str
    ) -> None: ...

    async def enroll_telegram_user(
        self,
        token_hash: bytes,
        pepper_key_id: str,
        telegram_user_id: int,
        now: datetime,
    ) -> EnrollmentOutcome: ...

    async def read_user_space_language(
        self, access_context: AccessContext
    ) -> str | None: ...

    async def set_user_space_language(
        self, access_context: AccessContext, language: str, now: datetime
    ) -> bool: ...


UpdateHandler = Callable[[UpdateTransaction], Awaitable[NewUpdateResult]]


class UpdateStore(Protocol):
    async def process_once(
        self,
        bot_id: int,
        update_id: int,
        occurred_at: datetime,
        handler: UpdateHandler,
    ) -> StoredUpdateReceipt: ...

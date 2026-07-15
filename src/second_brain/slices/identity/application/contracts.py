from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from second_brain.shared.i18n import Locale


@dataclass(frozen=True)
class AccessContext:
    user_id: UUID
    user_space_id: UUID


@dataclass(frozen=True)
class TelegramRecipient:
    telegram_user_id: int = field(repr=False)


class WorkerIdentityPort(Protocol):
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]: ...

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient: ...

    async def resolve_locale(self, access_context: AccessContext) -> Locale: ...


class LocaleResolver(Protocol):
    async def resolve_for_telegram_user(self, telegram_user_id: int) -> Locale: ...


@dataclass(frozen=True)
class PanelContext:
    locale: Locale
    is_admin: bool


class PanelContextResolver(Protocol):
    async def resolve_panel_context(self, telegram_user_id: int) -> PanelContext: ...


class UpdateTransaction(Protocol):
    """Published marker for work performed in an existing update transaction."""

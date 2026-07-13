from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.ports.repositories import (
    TelegramAccessContextResolver,
)


class ResolveAccessContext:
    """Resolves an internal context from a Telegram actor controlled by the server."""

    def __init__(self, resolver: TelegramAccessContextResolver) -> None:
        self._resolver = resolver

    async def execute(self, telegram_user_id: int) -> AccessContext | None:
        return await self._resolver.resolve_access_context(telegram_user_id)

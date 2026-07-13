from typing import Protocol

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import (
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.domain.entities import SearchRecord


class ExactSearchStore(Protocol):
    async def set_awaiting(self, command: SetAwaitingSearchCommand) -> None: ...

    async def cancel(self, access_context: AccessContext) -> None: ...

    async def lock_pending(self, access_context: AccessContext) -> bool: ...

    async def search(
        self,
        access_context: AccessContext,
        query: str,
        limit: int,
    ) -> tuple[SearchRecord, ...]: ...

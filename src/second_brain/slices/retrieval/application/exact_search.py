from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import (
    ConsumeSearchQueryCommand,
    SearchPanelResult,
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.ports.repositories import ExactSearchStore

SEARCH_LIMIT = 10


class ExactSearch:
    def __init__(self, store: ExactSearchStore) -> None:
        self._store = store

    async def set_awaiting(self, command: SetAwaitingSearchCommand) -> None:
        await self._store.set_awaiting(command)

    async def cancel(self, access_context: AccessContext) -> None:
        await self._store.cancel(access_context)

    async def consume_query(
        self, command: ConsumeSearchQueryCommand
    ) -> SearchPanelResult | None:
        if not await self._store.lock_pending(command.access_context):
            return None

        query = " ".join(command.query.split())
        if not query:
            return SearchPanelResult(items=(), query_required=True)

        items = await self._store.search(
            command.access_context,
            query,
            SEARCH_LIMIT,
        )
        await self._store.cancel(command.access_context)
        return SearchPanelResult(items=items, query_required=False)

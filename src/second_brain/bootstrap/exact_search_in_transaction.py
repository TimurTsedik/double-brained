from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresExactSearchWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    ConsumeSearchQueryCommand,
    ExactSearchPort,
    SearchPanelResult,
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.application.exact_search import ExactSearch


class ExactSearchInTransaction(ExactSearchPort):
    """Bootstrap composition for exact search inside an update transaction."""

    async def set_awaiting(
        self,
        command: SetAwaitingSearchCommand,
        transaction: UpdateTransaction,
    ) -> None:
        await _exact_search(transaction).set_awaiting(command)

    async def cancel(
        self,
        access_context: AccessContext,
        transaction: UpdateTransaction,
    ) -> None:
        await _exact_search(transaction).cancel(access_context)

    async def consume_query(
        self,
        command: ConsumeSearchQueryCommand,
        transaction: UpdateTransaction,
    ) -> SearchPanelResult | None:
        return await _exact_search(transaction).consume_query(command)


def _exact_search(transaction: UpdateTransaction) -> ExactSearch:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("exact search requires the PostgreSQL update transaction")
    return ExactSearch(PostgresExactSearchWriter(transaction.active_session))

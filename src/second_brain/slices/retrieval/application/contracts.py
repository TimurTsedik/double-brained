from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.retrieval.domain.entities import SearchRecord as SearchRecord


@dataclass(frozen=True)
class SetAwaitingSearchCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ConsumeSearchQueryCommand:
    access_context: AccessContext
    query: str = field(repr=False)


@dataclass(frozen=True)
class SearchPanelResult:
    items: tuple[SearchRecord, ...]
    query_required: bool


class ExactSearchPort(Protocol):
    async def set_awaiting(
        self,
        command: SetAwaitingSearchCommand,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def cancel(
        self,
        access_context: AccessContext,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def consume_query(
        self,
        command: ConsumeSearchQueryCommand,
        transaction: UpdateTransaction,
    ) -> SearchPanelResult | None: ...

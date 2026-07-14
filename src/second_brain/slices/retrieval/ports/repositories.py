from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
    SetAwaitingSearchCommand,
    StoreSemanticChunksCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    IndexingTarget,
    SearchRecord,
    SearchRecordType,
    SemanticMatch,
)


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


class SemanticIndexStore(Protocol):
    async def register_target(self, command: RegisterIndexingTargetCommand) -> None: ...

    async def read_target(
        self, access_context: AccessContext, processing_run_id: UUID
    ) -> IndexingTarget | None: ...

    async def existing_chunks(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
        index_version: int,
    ) -> tuple[tuple[int, str], ...]: ...

    async def insert_chunks(self, command: StoreSemanticChunksCommand) -> None: ...

    async def search_similar(
        self,
        access_context: AccessContext,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[SemanticMatch, ...]: ...

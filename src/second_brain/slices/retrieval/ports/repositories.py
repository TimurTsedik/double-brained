from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
    SetAwaitingSearchCommand,
    StoreSemanticChunksCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    DigestCounters,
    IndexingTarget,
    RecordView,
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

    async def replace_chunks_for_record(
        self, command: StoreSemanticChunksCommand
    ) -> None: ...

    async def search_similar(
        self,
        access_context: AccessContext,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[SemanticMatch, ...]: ...


class DigestStore(Protocol):
    async def count_records(
        self,
        access_context: AccessContext,
        start: datetime,
        end: datetime,
    ) -> DigestCounters: ...

    async def read_page(
        self,
        access_context: AccessContext,
        start: datetime,
        end: datetime,
        offset: int,
        limit: int,
    ) -> tuple[RecordView, ...]: ...


class RecordViewStore(Protocol):
    async def read_record(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
    ) -> RecordView | None: ...

    async def related_candidates(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
        limit: int,
    ) -> tuple[tuple[SearchRecordType, UUID], ...]: ...

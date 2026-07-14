from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    SucceedProcessingStepCommand,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_MODEL_NAME,
    INDEX_VERSION,
    IndexingOutcome,
    StoreSemanticChunksCommand,
)


class StaleSemanticIndexError(RuntimeError):
    """Existing projection chunks diverge from the freshly embedded set."""

    safe_error_code = "stale_semantic_index"

    def __init__(self) -> None:
        super().__init__("stale_semantic_index")


@dataclass(frozen=True)
class CompleteIndexingCommand:
    access_context: AccessContext = field(repr=False)
    step_id: UUID = field(repr=False)
    outcome: IndexingOutcome = field(repr=False)
    completed_at: datetime


class IndexingCompletionInTransaction:
    """Writes all chunks of one record atomically under the step lock:
    no existing rows -> insert, identical set -> idempotent no-op,
    any divergence -> StaleSemanticIndexError (never mixes chunk sets)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def complete(self, command: CompleteIndexingCommand) -> None:
        outcome = command.outcome
        async with self._session_factory() as session:
            async with session.begin():
                processing = PostgresProcessingWriter(session)
                target = await processing.lock_indexing_target(
                    command.access_context, command.step_id
                )
                writer = PostgresSemanticIndexWriter(session)
                existing = await writer.existing_chunks(
                    command.access_context,
                    outcome.record_kind,
                    outcome.record_id,
                    INDEX_VERSION,
                )
                expected = tuple(
                    (chunk.chunk_number, chunk.content_sha256)
                    for chunk in sorted(
                        outcome.chunks, key=lambda chunk: chunk.chunk_number
                    )
                )
                if existing and existing != expected:
                    raise StaleSemanticIndexError
                if not existing:
                    await writer.insert_chunks(
                        StoreSemanticChunksCommand(
                            access_context=command.access_context,
                            record_kind=outcome.record_kind,
                            record_id=outcome.record_id,
                            source_capture_event_id=target.capture_event_id,
                            chunks=outcome.chunks,
                            embedding_model=EMBEDDING_MODEL_NAME,
                            index_version=INDEX_VERSION,
                            # The record's own date, not the completion time:
                            # evidence chunks must carry when the record was
                            # created, matching the FTS path of retrieval.
                            created_at=outcome.created_at,
                            trace_id=target.trace_id,
                        )
                    )
                await processing.succeed_step(
                    SucceedProcessingStepCommand(
                        access_context=command.access_context,
                        step_id=command.step_id,
                        completed_at=command.completed_at,
                    )
                )

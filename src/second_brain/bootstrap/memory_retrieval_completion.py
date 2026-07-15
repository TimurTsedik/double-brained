from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.shared.secret_scan import contains_credential
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryWriter,
)
from second_brain.slices.memory.domain.entities import (
    EvidenceSnippet,
    MemoryRecordKind,
)
from second_brain.slices.memory.ports.repositories import (
    SnapshotEvidenceCommand,
    SucceedMemoryStepCommand,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresExactSearchWriter,
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import RetrieveMemoryCommand
from second_brain.slices.retrieval.application.hybrid_retrieval import (
    HybridMemoryRetrieval,
)
from second_brain.slices.retrieval.domain.entities import EvidenceChunk
from second_brain.slices.retrieval.ports.embedding import EmbeddingModel


@dataclass(frozen=True)
class CompleteMemoryRetrievalCommand:
    access_context: AccessContext = field(repr=False)
    step_id: UUID = field(repr=False)
    run_id: UUID = field(repr=False)
    completed_at: datetime


class MemoryRetrievalCompletionInTransaction:
    """Runs HybridMemoryRetrieval (retrieval slice's first prod consumer),
    drops any snippet that trips the shared credential scanner, snapshots the
    survivors with labels S1..Sn, and succeeds the retrieval step — all in one
    transaction so a crash rolls back a partial snapshot. An empty bundle (or an
    all-secret bundle) is a valid outcome: the snapshot stays empty and the step
    still SUCCEEDS; insufficiency is decided at reasoning without a provider."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_model: EmbeddingModel,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_model = embedding_model

    async def complete(self, command: CompleteMemoryRetrievalCommand) -> None:
        async with self._session_factory() as session, session.begin():
            writer = PostgresMemoryWriter(session)
            question = await writer.read_run_question(
                command.access_context, command.run_id
            )
            if question is None:
                raise LookupError("memory run question was not found")
            retrieval = HybridMemoryRetrieval(
                PostgresExactSearchWriter(session),
                PostgresSemanticIndexWriter(session),
                self._embedding_model,
            )
            bundle = await retrieval.retrieve(
                RetrieveMemoryCommand(
                    access_context=command.access_context,
                    question=question.question_text,
                    current_project_id=question.current_project_id,
                )
            )
            snippets = _labelled_snapshot(bundle.chunks)
            await writer.snapshot_evidence(
                SnapshotEvidenceCommand(
                    access_context=command.access_context,
                    run_id=command.run_id,
                    snippets=snippets,
                )
            )
            await writer.succeed_step(
                SucceedMemoryStepCommand(
                    access_context=command.access_context,
                    step_id=command.step_id,
                    completed_at=command.completed_at,
                )
            )


def _labelled_snapshot(
    chunks: tuple[EvidenceChunk, ...],
) -> tuple[EvidenceSnippet, ...]:
    snippets: list[EvidenceSnippet] = []
    for chunk in chunks:
        if contains_credential(chunk.text):
            # A snippet that looks like a user secret is never snapshotted and
            # therefore never reaches the reasoning provider.
            continue
        snippets.append(
            EvidenceSnippet(
                label=f"S{len(snippets) + 1}",
                record_kind=MemoryRecordKind(chunk.record_kind.value),
                record_id=chunk.record_id,
                source_capture_event_id=chunk.source_capture_event_id,
                created_at=chunk.created_at,
                text=chunk.text,
            )
        )
    return tuple(snippets)

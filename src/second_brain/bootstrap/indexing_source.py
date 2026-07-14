from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    IdeaModel,
    NoteModel,
    QuestionModel,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import IndexingSource
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.tasks.adapters.persistence.models import TaskModel


class IndexingTargetMismatchError(RuntimeError):
    """The registered target does not belong to the capture of its run."""

    safe_error_code = "indexing_target_mismatch"

    def __init__(self) -> None:
        super().__init__("indexing_target_mismatch")


@dataclass(frozen=True)
class ReadIndexingSourceCommand:
    access_context: AccessContext = field(repr=False)
    processing_run_id: UUID = field(repr=False)


_SOURCE_COLUMNS: Final[
    dict[
        SearchRecordType,
        tuple[
            InstrumentedAttribute[UUID],
            InstrumentedAttribute[UUID],
            InstrumentedAttribute[UUID],
            InstrumentedAttribute[str],
            InstrumentedAttribute[datetime],
        ],
    ]
] = {
    SearchRecordType.NOTE: (
        NoteModel.id,
        NoteModel.user_space_id,
        NoteModel.source_capture_event_id,
        NoteModel.text,
        NoteModel.created_at,
    ),
    SearchRecordType.TASK: (
        TaskModel.id,
        TaskModel.user_space_id,
        TaskModel.source_capture_event_id,
        TaskModel.title,
        TaskModel.created_at,
    ),
    SearchRecordType.IDEA: (
        IdeaModel.id,
        IdeaModel.user_space_id,
        IdeaModel.source_capture_event_id,
        IdeaModel.text,
        IdeaModel.created_at,
    ),
    SearchRecordType.DECISION: (
        DecisionModel.id,
        DecisionModel.user_space_id,
        DecisionModel.source_capture_event_id,
        DecisionModel.text,
        DecisionModel.created_at,
    ),
    SearchRecordType.QUESTION: (
        QuestionModel.id,
        QuestionModel.user_space_id,
        QuestionModel.source_capture_event_id,
        QuestionModel.text,
        QuestionModel.created_at,
    ),
}


class PostgresIndexingSourceReader:
    """Reads the explicitly registered indexing target of a scoped run and the
    text of exactly that record, verified against the run's capture event."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self, command: ReadIndexingSourceCommand) -> IndexingSource:
        async with self._session_factory() as session:
            async with session.begin():
                target = await PostgresSemanticIndexWriter(session).read_target(
                    command.access_context, command.processing_run_id
                )
                if target is None:
                    raise LookupError("indexing target was not found")
                id_column, space_column, source_column, text_column, created_column = (
                    _SOURCE_COLUMNS[target.record_kind]
                )
                row = (
                    await session.execute(
                        select(text_column, created_column).where(
                            id_column == target.record_id,
                            space_column == command.access_context.user_space_id,
                            source_column == target.capture_event_id,
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise IndexingTargetMismatchError
                record_text: str
                record_created_at: datetime
                record_text, record_created_at = row._tuple()
                return IndexingSource(
                    record_kind=target.record_kind,
                    record_id=target.record_id,
                    text=record_text,
                    created_at=record_created_at,
                )

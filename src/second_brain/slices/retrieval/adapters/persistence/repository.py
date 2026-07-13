from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ColumnElement,
    case,
    cast,
    delete,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import Select

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    IdeaModel,
    NoteModel,
    QuestionModel,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from second_brain.slices.retrieval.application.contracts import (
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    MatchQuality,
    SearchRecord,
    SearchRecordType,
)
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.tasks.domain.entities import TaskStatus

FTS_CONFIGURATION: ColumnElement[Any] = literal_column("'simple'::regconfig")
MIN_SUBSTRING_LENGTH = 3


class PostgresExactSearchWriter:
    """Owns pending mode and reads canonical typed records in one transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_awaiting(self, command: SetAwaitingSearchCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        statement = (
            postgresql_insert(PendingSearchModeModel)
            .values(
                user_space_id=command.access_context.user_space_id,
                updated_at=command.updated_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_update(
                index_elements=[PendingSearchModeModel.user_space_id],
                set_={
                    "updated_at": command.updated_at,
                    "trace_id": command.trace_id,
                },
            )
        )
        await self._session.execute(statement)

    async def cancel(self, access_context: AccessContext) -> None:
        await _set_user_space_scope(self._session, access_context)
        await self._session.execute(
            delete(PendingSearchModeModel).where(
                PendingSearchModeModel.user_space_id == access_context.user_space_id
            )
        )

    async def lock_pending(self, access_context: AccessContext) -> bool:
        await _set_user_space_scope(self._session, access_context)
        pending_id = await self._session.scalar(
            select(PendingSearchModeModel.user_space_id)
            .where(PendingSearchModeModel.user_space_id == access_context.user_space_id)
            .with_for_update()
        )
        return pending_id is not None

    async def search(
        self,
        access_context: AccessContext,
        query: str,
        limit: int,
    ) -> tuple[SearchRecord, ...]:
        await _set_user_space_scope(self._session, access_context)
        task_completed = (TaskModel.status == TaskStatus.COMPLETED).label(
            "task_completed"
        )
        not_a_task = cast(literal(None), Boolean).label("task_completed")
        branches = (
            _search_branch(
                SearchRecordType.NOTE,
                NoteModel.id,
                NoteModel.user_space_id,
                NoteModel.text,
                NoteModel.source_capture_event_id,
                NoteModel.created_at,
                not_a_task,
                access_context.user_space_id,
                query,
            ),
            _search_branch(
                SearchRecordType.TASK,
                TaskModel.id,
                TaskModel.user_space_id,
                TaskModel.title,
                TaskModel.source_capture_event_id,
                TaskModel.created_at,
                task_completed,
                access_context.user_space_id,
                query,
            ),
            _search_branch(
                SearchRecordType.IDEA,
                IdeaModel.id,
                IdeaModel.user_space_id,
                IdeaModel.text,
                IdeaModel.source_capture_event_id,
                IdeaModel.created_at,
                not_a_task,
                access_context.user_space_id,
                query,
            ),
            _search_branch(
                SearchRecordType.DECISION,
                DecisionModel.id,
                DecisionModel.user_space_id,
                DecisionModel.text,
                DecisionModel.source_capture_event_id,
                DecisionModel.created_at,
                not_a_task,
                access_context.user_space_id,
                query,
            ),
            _search_branch(
                SearchRecordType.QUESTION,
                QuestionModel.id,
                QuestionModel.user_space_id,
                QuestionModel.text,
                QuestionModel.source_capture_event_id,
                QuestionModel.created_at,
                not_a_task,
                access_context.user_space_id,
                query,
            ),
        )
        combined = union_all(*branches).subquery()
        statement = (
            select(combined)
            .order_by(
                combined.c.match_quality,
                combined.c.created_at.desc(),
                combined.c.record_type,
                combined.c.id,
            )
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).mappings()
        return tuple(_to_record(row) for row in rows)


def _search_branch(
    record_type: SearchRecordType,
    id_column: InstrumentedAttribute[UUID],
    user_space_column: InstrumentedAttribute[UUID],
    content_column: InstrumentedAttribute[str],
    source_column: InstrumentedAttribute[UUID],
    created_column: InstrumentedAttribute[Any],
    task_completed: ColumnElement[Any],
    user_space_id: UUID,
    query: str,
) -> Select[Any]:
    substring_match: ColumnElement[bool]
    if len(query) >= MIN_SUBSTRING_LENGTH:
        substring_match = content_column.icontains(query, autoescape=True)
    else:
        substring_match = false()
    full_text_match = func.to_tsvector(FTS_CONFIGURATION, content_column).bool_op("@@")(
        func.websearch_to_tsquery(FTS_CONFIGURATION, query)
    )
    quality = case(
        (substring_match, MatchQuality.SUBSTRING.value),
        else_=MatchQuality.FULL_TEXT.value,
    ).label("match_quality")
    return select(
        literal(record_type.value).label("record_type"),
        id_column.label("id"),
        content_column.label("text"),
        source_column.label("source_capture_event_id"),
        created_column.label("created_at"),
        task_completed,
        quality,
    ).where(
        user_space_column == user_space_id,
        or_(substring_match, full_text_match),
    )


def _to_record(row: Any) -> SearchRecord:
    return SearchRecord(
        id=row["id"],
        record_type=SearchRecordType(row["record_type"]),
        text=row["text"],
        source_capture_event_id=row["source_capture_event_id"],
        created_at=row["created_at"],
        task_completed=row["task_completed"],
        match_quality=MatchQuality(row["match_quality"]),
    )


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

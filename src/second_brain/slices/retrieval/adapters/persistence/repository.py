from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    ColumnElement,
    and_,
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
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased
from sqlalchemy.sql import Select

from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.capture.domain.entities import CaptureSourceKind
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    IdeaModel,
    NoteModel,
    QuestionModel,
)
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingRunModel,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
    PendingSearchModeModel,
    SemanticDocumentModel,
)
from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_MODEL_NAME,
    INDEX_VERSION,
    RegisterIndexingTargetCommand,
    SetAwaitingSearchCommand,
    StoreSemanticChunksCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    DigestCounters,
    IndexingTarget,
    MatchQuality,
    RecordView,
    SearchRecord,
    SearchRecordType,
    SemanticMatch,
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
            ).where(~_completed_alarm_task()),
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


class PostgresSemanticIndexWriter:
    """Stores the semantic projection and indexing targets in a caller-owned
    transaction. The no-rows/matched/diverged completion policy lives above."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_target(self, command: RegisterIndexingTargetCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        statement = (
            postgresql_insert(IndexingTargetModel)
            .values(
                processing_run_id=command.processing_run_id,
                user_space_id=command.access_context.user_space_id,
                record_kind=command.record_kind,
                record_id=command.record_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_nothing(
                index_elements=[IndexingTargetModel.processing_run_id]
            )
        )
        await self._session.execute(statement)

    async def read_target(
        self, access_context: AccessContext, processing_run_id: UUID
    ) -> IndexingTarget | None:
        await _set_user_space_scope(self._session, access_context)
        statement = (
            select(
                IndexingTargetModel.record_kind,
                IndexingTargetModel.record_id,
                ProcessingRunModel.capture_event_id,
            )
            .select_from(IndexingTargetModel)
            .join(
                ProcessingRunModel,
                and_(
                    ProcessingRunModel.id == IndexingTargetModel.processing_run_id,
                    ProcessingRunModel.user_space_id
                    == IndexingTargetModel.user_space_id,
                ),
            )
            .where(
                IndexingTargetModel.processing_run_id == processing_run_id,
                IndexingTargetModel.user_space_id == access_context.user_space_id,
            )
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return IndexingTarget(
            record_kind=row.record_kind,
            record_id=row.record_id,
            capture_event_id=row.capture_event_id,
        )

    async def existing_chunks(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
        index_version: int,
    ) -> tuple[tuple[int, str], ...]:
        await _set_user_space_scope(self._session, access_context)
        statement = (
            select(
                SemanticDocumentModel.chunk_number,
                SemanticDocumentModel.content_sha256,
            )
            .where(
                SemanticDocumentModel.user_space_id == access_context.user_space_id,
                SemanticDocumentModel.source_kind == record_kind,
                SemanticDocumentModel.source_record_id == record_id,
                SemanticDocumentModel.index_version == index_version,
            )
            .order_by(SemanticDocumentModel.chunk_number)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple((row.chunk_number, row.content_sha256) for row in rows)

    async def insert_chunks(self, command: StoreSemanticChunksCommand) -> None:
        if not command.chunks:
            # An empty batch would compile to INSERT ... DEFAULT VALUES.
            return
        await _set_user_space_scope(self._session, command.access_context)
        statement = (
            postgresql_insert(SemanticDocumentModel)
            .values(
                [
                    {
                        "id": uuid4(),
                        "user_space_id": command.access_context.user_space_id,
                        "source_kind": command.record_kind,
                        "source_record_id": command.record_id,
                        "source_capture_event_id": command.source_capture_event_id,
                        "chunk_number": chunk.chunk_number,
                        "content_sha256": chunk.content_sha256,
                        "chunk_text": chunk.text,
                        "embedding_model": command.embedding_model,
                        "index_version": command.index_version,
                        "embedding": list(chunk.embedding),
                        "created_at": command.created_at,
                        "trace_id": command.trace_id,
                    }
                    for chunk in command.chunks
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SemanticDocumentModel.user_space_id,
                    SemanticDocumentModel.source_kind,
                    SemanticDocumentModel.source_record_id,
                    SemanticDocumentModel.index_version,
                    SemanticDocumentModel.chunk_number,
                ]
            )
        )
        await self._session.execute(statement)

    async def search_similar(
        self,
        access_context: AccessContext,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[SemanticMatch, ...]:
        await _set_user_space_scope(self._session, access_context)
        distance = SemanticDocumentModel.embedding.cosine_distance(list(query_vector))
        # Чанки завершённой «задачи-будильника» — такой же шум, как её строка
        # в точном поиске: скрываем их из векторных кандидатов тем же правилом.
        alarm_task_source = (
            select(literal(1))
            .where(
                TaskModel.id == SemanticDocumentModel.source_record_id,
                TaskModel.user_space_id == SemanticDocumentModel.user_space_id,
                _completed_alarm_task(),
            )
            .exists()
        )
        statement = (
            select(
                SemanticDocumentModel.source_kind,
                SemanticDocumentModel.source_record_id,
                SemanticDocumentModel.source_capture_event_id,
                SemanticDocumentModel.chunk_number,
                SemanticDocumentModel.chunk_text,
                SemanticDocumentModel.created_at,
            )
            .where(
                SemanticDocumentModel.user_space_id == access_context.user_space_id,
                SemanticDocumentModel.index_version == INDEX_VERSION,
                SemanticDocumentModel.embedding_model == EMBEDDING_MODEL_NAME,
                ~and_(
                    SemanticDocumentModel.source_kind == SearchRecordType.TASK,
                    alarm_task_source,
                ),
            )
            .order_by(
                distance,
                SemanticDocumentModel.source_kind,
                SemanticDocumentModel.source_record_id,
                SemanticDocumentModel.chunk_number,
            )
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(
            SemanticMatch(
                record_kind=row.source_kind,
                record_id=row.source_record_id,
                source_capture_event_id=row.source_capture_event_id,
                chunk_number=row.chunk_number,
                text=row.chunk_text,
                created_at=row.created_at,
            )
            for row in rows
        )


_KNOWLEDGE_MODELS: dict[
    SearchRecordType,
    type[NoteModel] | type[IdeaModel] | type[DecisionModel] | type[QuestionModel],
] = {
    SearchRecordType.NOTE: NoteModel,
    SearchRecordType.IDEA: IdeaModel,
    SearchRecordType.DECISION: DecisionModel,
    SearchRecordType.QUESTION: QuestionModel,
}


def _image_source_exists(
    source_column: InstrumentedAttribute[UUID],
) -> ColumnElement[bool]:
    # «У записи есть изображение-источник»: EXISTS по capture_events того же
    # forced-RLS сеанса (чужие пространства невидимы), корреляция по id
    # источника записи.
    return (
        select(CaptureEventModel.id)
        .where(
            CaptureEventModel.id == source_column,
            CaptureEventModel.source_kind == CaptureSourceKind.IMAGE,
        )
        .exists()
    )


class PostgresRecordViewReader:
    """Читает каноническую запись по (типу, uuid, пространству) под RLS и считает
    кандидатов «похожего» по чанкам ТЕКУЩИХ embedding_model+INDEX_VERSION."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read_record(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
    ) -> RecordView | None:
        await _set_user_space_scope(self._session, access_context)
        if record_kind is SearchRecordType.TASK:
            task_row = (
                await self._session.execute(
                    select(
                        TaskModel.title,
                        TaskModel.created_at,
                        TaskModel.status,
                        _image_source_exists(TaskModel.source_capture_event_id).label(
                            "has_image_source"
                        ),
                    ).where(
                        TaskModel.id == record_id,
                        TaskModel.user_space_id == access_context.user_space_id,
                    )
                )
            ).one_or_none()
            if task_row is None:
                return None
            return RecordView(
                id=record_id,
                record_type=record_kind,
                text=task_row.title,
                created_at=task_row.created_at,
                task_completed=task_row.status == TaskStatus.COMPLETED,
                has_image_source=bool(task_row.has_image_source),
            )
        model = _KNOWLEDGE_MODELS[record_kind]
        row = (
            await self._session.execute(
                select(
                    model.text,
                    model.created_at,
                    _image_source_exists(model.source_capture_event_id).label(
                        "has_image_source"
                    ),
                ).where(
                    model.id == record_id,
                    model.user_space_id == access_context.user_space_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return RecordView(
            id=record_id,
            record_type=record_kind,
            text=row.text,
            created_at=row.created_at,
            task_completed=None,
            has_image_source=bool(row.has_image_source),
        )

    async def image_attachment(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
    ) -> tuple[str, str | None] | None:
        """(telegram_file_id, storage_key) image-источника записи или None.

        storage_key None — оригинал ещё не скачан download-шагом воркера
        (fast path по file_id остаётся единственной попыткой показа).
        """
        await _set_user_space_scope(self._session, access_context)
        model = (
            TaskModel
            if record_kind is SearchRecordType.TASK
            else _KNOWLEDGE_MODELS[record_kind]
        )
        row = (
            await self._session.execute(
                select(
                    TelegramAttachmentModel.telegram_file_id,
                    TelegramAttachmentModel.storage_key,
                )
                .join(
                    model,
                    and_(
                        model.source_capture_event_id
                        == TelegramAttachmentModel.capture_event_id,
                        model.user_space_id == TelegramAttachmentModel.user_space_id,
                    ),
                )
                .where(
                    model.id == record_id,
                    model.user_space_id == access_context.user_space_id,
                    TelegramAttachmentModel.kind == CaptureSourceKind.IMAGE,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return row.telegram_file_id, row.storage_key

    async def related_candidates(
        self,
        access_context: AccessContext,
        record_kind: SearchRecordType,
        record_id: UUID,
        limit: int,
    ) -> tuple[tuple[SearchRecordType, UUID], ...]:
        # Вектор запроса — СОБСТВЕННЫЕ чанки записи (без нового вызова эмбеддера),
        # только текущих embedding_model+INDEX_VERSION: нет таких чанков — cross
        # join пуст и секции «похожего» не будет. Ранжирование детерминированное:
        # минимальная дистанция по всем своим чанкам (дедуп до записей через
        # GROUP BY), затем kind и id.
        await _set_user_space_scope(self._session, access_context)
        own_chunks = (
            select(SemanticDocumentModel.embedding)
            .where(
                SemanticDocumentModel.user_space_id == access_context.user_space_id,
                SemanticDocumentModel.source_kind == record_kind,
                SemanticDocumentModel.source_record_id == record_id,
                SemanticDocumentModel.embedding_model == EMBEDDING_MODEL_NAME,
                SemanticDocumentModel.index_version == INDEX_VERSION,
            )
            .subquery()
        )
        neighbour = aliased(SemanticDocumentModel)
        distance = neighbour.embedding.cosine_distance(own_chunks.c.embedding)
        statement = (
            select(neighbour.source_kind, neighbour.source_record_id)
            .join(own_chunks, true())
            .where(
                neighbour.user_space_id == access_context.user_space_id,
                neighbour.embedding_model == EMBEDDING_MODEL_NAME,
                neighbour.index_version == INDEX_VERSION,
                or_(
                    neighbour.source_kind != record_kind,
                    neighbour.source_record_id != record_id,
                ),
            )
            .group_by(neighbour.source_kind, neighbour.source_record_id)
            .order_by(
                func.min(distance),
                neighbour.source_kind,
                neighbour.source_record_id,
            )
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple((row.source_kind, row.source_record_id) for row in rows)


class PostgresDigestReader:
    """Счётчики и страница сводки за окно `start <= created_at <= end` под RLS.

    Оба чтения — по одному запросу (UNION по типовым таблицам, НЕ по запросу на
    запись) и по одному и тому же окну снимка: записи, созданные после `end`
    (as_of), не видны ни счётчикам, ни страницам. Порядок детерминированный:
    created_at DESC, затем тип и id — страницы стабильны при равных датах.
    Завершённые задачи-будильники (`_completed_alarm_task`) скрыты из ОБОИХ
    чтений одинаково — счётчики никогда не расходятся со списком.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_records(
        self,
        access_context: AccessContext,
        start: datetime,
        end: datetime,
    ) -> DigestCounters:
        await _set_user_space_scope(self._session, access_context)
        completed_tasks = func.count().filter(TaskModel.status == TaskStatus.COMPLETED)
        branches = tuple(
            select(
                literal(source.record_type.value).label("record_type"),
                func.count().label("total"),
                (
                    completed_tasks
                    if source.record_type is SearchRecordType.TASK
                    else literal(0)
                ).label("completed"),
            ).where(
                source.user_space_column == access_context.user_space_id,
                source.created_column >= start,
                source.created_column <= end,
                *_digest_task_filters(source),
            )
            for source in _digest_sources()
        )
        rows = (await self._session.execute(union_all(*branches))).all()
        by_type = {row.record_type: row for row in rows}

        def total_of(record_type: SearchRecordType) -> int:
            row = by_type.get(record_type.value)
            return int(row.total) if row is not None else 0

        task_row = by_type.get(SearchRecordType.TASK.value)
        return DigestCounters(
            notes=total_of(SearchRecordType.NOTE),
            tasks=total_of(SearchRecordType.TASK),
            tasks_completed=int(task_row.completed) if task_row is not None else 0,
            ideas=total_of(SearchRecordType.IDEA),
            decisions=total_of(SearchRecordType.DECISION),
            questions=total_of(SearchRecordType.QUESTION),
        )

    async def read_page(
        self,
        access_context: AccessContext,
        start: datetime,
        end: datetime,
        offset: int,
        limit: int,
    ) -> tuple[RecordView, ...]:
        await _set_user_space_scope(self._session, access_context)
        task_completed = (TaskModel.status == TaskStatus.COMPLETED).label(
            "task_completed"
        )
        not_a_task = cast(literal(None), Boolean).label("task_completed")
        branches = tuple(
            select(
                literal(source.record_type.value).label("record_type"),
                source.id_column.label("id"),
                source.content_column.label("text"),
                source.created_column.label("created_at"),
                (
                    task_completed
                    if source.record_type is SearchRecordType.TASK
                    else not_a_task
                ),
                # Метка 📷 — коррелированным EXISTS в том же union-запросе.
                _image_source_exists(source.source_column).label("has_image_source"),
            ).where(
                source.user_space_column == access_context.user_space_id,
                source.created_column >= start,
                source.created_column <= end,
                *_digest_task_filters(source),
            )
            for source in _digest_sources()
        )
        combined = union_all(*branches).subquery()
        statement = (
            select(combined)
            .order_by(
                combined.c.created_at.desc(),
                combined.c.record_type,
                combined.c.id,
            )
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).mappings()
        return tuple(
            RecordView(
                id=row["id"],
                record_type=SearchRecordType(row["record_type"]),
                text=row["text"],
                created_at=row["created_at"],
                task_completed=row["task_completed"],
                has_image_source=bool(row["has_image_source"]),
            )
            for row in rows
        )


@dataclass(frozen=True)
class _DigestSource:
    record_type: SearchRecordType
    id_column: InstrumentedAttribute[UUID]
    user_space_column: InstrumentedAttribute[UUID]
    content_column: InstrumentedAttribute[str]
    created_column: InstrumentedAttribute[Any]
    source_column: InstrumentedAttribute[UUID]


def _digest_sources() -> tuple[_DigestSource, ...]:
    return (
        _DigestSource(
            SearchRecordType.NOTE,
            NoteModel.id,
            NoteModel.user_space_id,
            NoteModel.text,
            NoteModel.created_at,
            NoteModel.source_capture_event_id,
        ),
        _DigestSource(
            SearchRecordType.TASK,
            TaskModel.id,
            TaskModel.user_space_id,
            TaskModel.title,
            TaskModel.created_at,
            TaskModel.source_capture_event_id,
        ),
        _DigestSource(
            SearchRecordType.IDEA,
            IdeaModel.id,
            IdeaModel.user_space_id,
            IdeaModel.text,
            IdeaModel.created_at,
            IdeaModel.source_capture_event_id,
        ),
        _DigestSource(
            SearchRecordType.DECISION,
            DecisionModel.id,
            DecisionModel.user_space_id,
            DecisionModel.text,
            DecisionModel.created_at,
            DecisionModel.source_capture_event_id,
        ),
        _DigestSource(
            SearchRecordType.QUESTION,
            QuestionModel.id,
            QuestionModel.user_space_id,
            QuestionModel.text,
            QuestionModel.created_at,
            QuestionModel.source_capture_event_id,
        ),
    )


def _digest_task_filters(source: _DigestSource) -> tuple[ColumnElement[bool], ...]:
    """Доп. условия ветки сводки: из задач скрываем ВСЕ будильники — задачу с
    напоминанием любого статуса (и активную, и завершённую).

    Сводка — «чистый список записей за период»; будильник там операционный
    шум независимо от того, сработал он или нет. Обычная задача без
    напоминания в сводке остаётся. Предикат `_alarm_task()` один и тот же в
    счётчиках и в странице — расхождение между ними исключено по построению.
    (Поиск и выдача памяти скрывают лишь ЗАВЕРШЁННЫЕ будильники — активный там
    нужно находить; см. `_completed_alarm_task()`.)
    """
    if source.record_type is SearchRecordType.TASK:
        return (~_alarm_task(),)
    return ()


def _alarm_task() -> ColumnElement[bool]:
    """«Задача-будильник» («позвонить Ави в 11:53»): на задачу есть
    напоминание любого статуса. Предикат по user_space_id внутри EXISTS
    повторяет RLS-границу."""
    return (
        select(literal(1))
        .where(
            ReminderModel.source_task_id == TaskModel.id,
            ReminderModel.user_space_id == TaskModel.user_space_id,
        )
        .exists()
    )


def _completed_alarm_task() -> ColumnElement[bool]:
    """ЗАВЕРШЁННАЯ задача-будильник: после выполнения это шум — точный поиск и
    выдача памяти её скрывают (в отличие от сводки, где скрыт будильник любого
    статуса). Активный будильник в поиске/памяти остаётся находимым."""
    return and_(TaskModel.status == TaskStatus.COMPLETED, _alarm_task())


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
        # Метка 📷 — коррелированным EXISTS в ТОМ ЖЕ запросе (не по запросу
        # на строку результата).
        _image_source_exists(source_column).label("has_image_source"),
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
        has_image_source=bool(row["has_image_source"]),
    )


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

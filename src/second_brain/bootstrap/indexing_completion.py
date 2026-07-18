from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.bootstrap.indexing_source import (
    SOURCE_COLUMNS,
    record_text_sha256,
)
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


@dataclass(frozen=True)
class CompleteIndexingCommand:
    access_context: AccessContext = field(repr=False)
    step_id: UUID = field(repr=False)
    outcome: IndexingOutcome = field(repr=False)
    completed_at: datetime


class IndexingCompletionInTransaction:
    """Атомарная запись проекции записи под замком шага (спека §3.3).

    Эмбеддинги посчитаны ДО этой транзакции (в воркере); здесь в ОДНОЙ
    транзакции: сверка с ТЕКУЩИМ текстом записи + delete прежних чанков +
    insert полного нового набора + succeed шага. Первая индексация и
    пере-индексация после правки — один путь; ретрай после сбоя идемпотентен
    по содержимому; конкурентный поиск видит старый набор до коммита и новый
    после — смешанных наборов и пустого окна нет.

    Гонка «поздняя индексация против правки»: шаг мог прочитать текст, после
    чего запись правится и её reindex-прогон уже записал свежие чанки. Поздний
    устаревший результат (sha исходного текста ≠ sha текущего) чанки НЕ
    пишет — шаг просто завершается: актуальный прогон текущий текст уже
    отразил или отразит своим шагом. Сверка держит строку записи под
    FOR UPDATE: конкурентная правка сериализуется ПОСЛЕ этого completion (её
    reindex тогда заведомо впереди), а закоммиченная ДО — гарантированно видна
    сверке; окна «прочитал старый текст, а правка успела прокоммититься и
    переиндексироваться до моего commit» не существует.
    """

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
                if await self._outcome_matches_current_text(
                    session, command, target.capture_event_id
                ):
                    await PostgresSemanticIndexWriter(
                        session
                    ).replace_chunks_for_record(
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

    async def _outcome_matches_current_text(
        self,
        session: AsyncSession,
        command: CompleteIndexingCommand,
        capture_event_id: UUID,
    ) -> bool:
        """Перечитать ТЕКУЩИЙ текст записи в ЭТОЙ транзакции и сверить sha."""
        outcome = command.outcome
        id_column, space_column, source_column, text_column, _ = SOURCE_COLUMNS[
            outcome.record_kind
        ]
        current_text = await session.scalar(
            select(text_column)
            .where(
                id_column == outcome.record_id,
                space_column == command.access_context.user_space_id,
                source_column == capture_event_id,
            )
            # Замок строки записи: правка (UPDATE той же строки) либо видна
            # сверке (закоммичена раньше), либо ждёт наш commit и её reindex
            # идёт после — устаревший результат не может лечь ПОВЕРХ свежего.
            .with_for_update()
        )
        if current_text is None:
            return False
        return record_text_sha256(current_text) == outcome.content_sha256

from uuid import UUID, uuid4

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    DecisionProvenanceModel,
    IdeaModel,
    IdeaProvenanceModel,
    NoteModel,
    NoteProvenanceModel,
    QuestionModel,
    QuestionProvenanceModel,
)
from second_brain.slices.knowledge.application.contracts import (
    CreateDecisionCommand,
    CreateIdeaCommand,
    CreateNoteCommand,
    CreateQuestionCommand,
    UpdateKnowledgeTextCommand,
)
from second_brain.slices.knowledge.domain.entities import (
    Decision,
    Idea,
    KnowledgeRecordKind,
    Note,
    Question,
)

_KNOWLEDGE_MODELS: dict[
    KnowledgeRecordKind,
    type[NoteModel] | type[IdeaModel] | type[DecisionModel] | type[QuestionModel],
] = {
    KnowledgeRecordKind.NOTE: NoteModel,
    KnowledgeRecordKind.IDEA: IdeaModel,
    KnowledgeRecordKind.DECISION: DecisionModel,
    KnowledgeRecordKind.QUESTION: QuestionModel,
}


class PostgresNoteRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, command: CreateNoteCommand) -> Note:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresKnowledgeWriter(session).create_note(command)


class PostgresIdeaRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, command: CreateIdeaCommand) -> Idea:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresKnowledgeWriter(session).create_idea(command)


class PostgresDecisionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, command: CreateDecisionCommand) -> Decision:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresKnowledgeWriter(session).create_decision(command)


class PostgresQuestionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, command: CreateQuestionCommand) -> Question:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresKnowledgeWriter(session).create_question(command)


class PostgresKnowledgeWriter:
    """Writes typed knowledge records through a transaction owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_note(self, command: CreateNoteCommand) -> Note:
        await _set_user_space_scope(self._session, command.access_context)
        record_id = uuid4()
        model = NoteModel(
            id=record_id,
            user_space_id=command.access_context.user_space_id,
            text=command.text,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(
            NoteProvenanceModel(
                note_id=record_id,
                source_capture_event_id=command.source_capture_event_id,
                user_space_id=command.access_context.user_space_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        await self._session.flush()
        return Note(
            id=model.id,
            user_space_id=model.user_space_id,
            text=model.text,
            source_capture_event_id=model.source_capture_event_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
            trace_id=model.trace_id,
        )

    async def create_idea(self, command: CreateIdeaCommand) -> Idea:
        await _set_user_space_scope(self._session, command.access_context)
        record_id = uuid4()
        model = IdeaModel(
            id=record_id,
            user_space_id=command.access_context.user_space_id,
            text=command.text,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(
            IdeaProvenanceModel(
                idea_id=record_id,
                source_capture_event_id=command.source_capture_event_id,
                user_space_id=command.access_context.user_space_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        await self._session.flush()
        return Idea(
            id=model.id,
            user_space_id=model.user_space_id,
            text=model.text,
            source_capture_event_id=model.source_capture_event_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
            trace_id=model.trace_id,
        )

    async def create_decision(self, command: CreateDecisionCommand) -> Decision:
        await _set_user_space_scope(self._session, command.access_context)
        record_id = uuid4()
        model = DecisionModel(
            id=record_id,
            user_space_id=command.access_context.user_space_id,
            text=command.text,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(
            DecisionProvenanceModel(
                decision_id=record_id,
                source_capture_event_id=command.source_capture_event_id,
                user_space_id=command.access_context.user_space_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        await self._session.flush()
        return Decision(
            id=model.id,
            user_space_id=model.user_space_id,
            text=model.text,
            source_capture_event_id=model.source_capture_event_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
            trace_id=model.trace_id,
        )

    async def create_question(self, command: CreateQuestionCommand) -> Question:
        await _set_user_space_scope(self._session, command.access_context)
        record_id = uuid4()
        model = QuestionModel(
            id=record_id,
            user_space_id=command.access_context.user_space_id,
            text=command.text,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(
            QuestionProvenanceModel(
                question_id=record_id,
                source_capture_event_id=command.source_capture_event_id,
                user_space_id=command.access_context.user_space_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        await self._session.flush()
        return Question(
            id=model.id,
            user_space_id=model.user_space_id,
            text=model.text,
            source_capture_event_id=model.source_capture_event_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
            trace_id=model.trace_id,
        )

    async def update_text(self, command: UpdateKnowledgeTextCommand) -> UUID | None:
        """Правка (S3): заменить text + бампнуть updated_at и edited_at строго
        в СВОЁМ пространстве (owner-предикат в WHERE поверх forced RLS).
        edited_at ставится ТОЛЬКО здесь — по нему показ метит «(изменено)».

        Возвращает source_capture_event_id правленой записи (нужен
        пере-индексации) или None, если записи нет / она чужая — вызывающий
        трактует это как несостоявшуюся правку.
        """
        await _set_user_space_scope(self._session, command.access_context)
        model = _KNOWLEDGE_MODELS[command.record_kind]
        return await self._session.scalar(
            update(model)
            .where(
                model.id == command.record_id,
                model.user_space_id == command.access_context.user_space_id,
            )
            .values(
                text=command.text,
                updated_at=command.updated_at,
                edited_at=command.updated_at,
            )
            .returning(model.source_capture_event_id)
        )


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

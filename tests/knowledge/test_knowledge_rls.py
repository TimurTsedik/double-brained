from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
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
from second_brain.slices.knowledge.adapters.persistence.repository import (
    PostgresDecisionRepository,
    PostgresIdeaRepository,
    PostgresNoteRepository,
    PostgresQuestionRepository,
)
from second_brain.slices.knowledge.application.contracts import (
    CreateDecisionCommand,
    CreateIdeaCommand,
    CreateNoteCommand,
    CreateQuestionCommand,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)


@dataclass(frozen=True)
class RecordKind:
    model: type[object]
    provenance_model: type[object]
    repository_factory: Callable[[AsyncEngine], object]
    command_factory: Callable[[AccessContext, UUID], object]


def _repository(repository_type: type[object]) -> Callable[[AsyncEngine], object]:
    def create(engine: AsyncEngine) -> object:
        return repository_type(create_session_factory(engine))

    return create


def _command(command_type: type[object]) -> Callable[[AccessContext, UUID], object]:
    def create(access_context: AccessContext, source_capture_event_id: UUID) -> object:
        return command_type(
            access_context=access_context,
            text="  exact private text  ",
            source_capture_event_id=source_capture_event_id,
            created_at=NOW,
            trace_id="1" * 32,
        )

    return create


RECORD_KINDS = [
    RecordKind(
        NoteModel,
        NoteProvenanceModel,
        _repository(PostgresNoteRepository),
        _command(CreateNoteCommand),
    ),
    RecordKind(
        IdeaModel,
        IdeaProvenanceModel,
        _repository(PostgresIdeaRepository),
        _command(CreateIdeaCommand),
    ),
    RecordKind(
        DecisionModel,
        DecisionProvenanceModel,
        _repository(PostgresDecisionRepository),
        _command(CreateDecisionCommand),
    ),
    RecordKind(
        QuestionModel,
        QuestionProvenanceModel,
        _repository(PostgresQuestionRepository),
        _command(CreateQuestionCommand),
    ),
]


@pytest_asyncio.fixture(autouse=True)
async def reset_knowledge_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                {
                    "id": ACCESS_A.user_id,
                    # Пространство A = admin, B = member: admin НЕ суперпользователь,
                    # RLS изолирует по user_space_id, не по роли.
                    "role": "admin",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": ACCESS_A.user_space_id,
                    "owner_user_id": ACCESS_A.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": ACCESS_B.user_space_id,
                    "owner_user_id": ACCESS_B.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )


def capture_command(
    access_context: AccessContext, update_id: int
) -> CaptureTextCommand:
    return CaptureTextCommand(
        access_context=access_context,
        bot_id=100,
        telegram_update_id=update_id,
        telegram_message_id=update_id + 1000,
        raw_text="source",
        received_at=NOW,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", RECORD_KINDS)
async def test_knowledge_records_and_provenance_are_scoped_to_their_user_space(
    kind: RecordKind, engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))
    source_a = await capture_repository.create(capture_command(ACCESS_A, 100))
    source_b = await capture_repository.create(capture_command(ACCESS_B, 101))
    repository = kind.repository_factory(engine)
    record_a = await repository.create(kind.command_factory(ACCESS_A, source_a.id))
    await repository.create(kind.command_factory(ACCESS_B, source_b.id))

    await _set_scope(session, ACCESS_A)
    assert (await session.scalars(select(kind.model.id))).all() == [record_a.id]
    assert (
        await session.scalars(select(kind.provenance_model.source_capture_event_id))
    ).all() == [source_a.id]
    assert await session.scalar(select(func.count()).select_from(kind.model)) == 1
    assert (
        await session.scalar(select(func.count()).select_from(kind.provenance_model))
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", RECORD_KINDS)
async def test_knowledge_records_are_scoped_from_the_member_side(
    kind: RecordKind, engine: AsyncEngine, session: AsyncSession
) -> None:
    # Реципрокно: под scope member'а (B) видна только его запись — записи admin'а
    # (A) не читаются. Приватность в обе стороны, admin НЕ суперпользователь.
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))
    source_a = await capture_repository.create(capture_command(ACCESS_A, 102))
    source_b = await capture_repository.create(capture_command(ACCESS_B, 103))
    repository = kind.repository_factory(engine)
    await repository.create(kind.command_factory(ACCESS_A, source_a.id))
    record_b = await repository.create(kind.command_factory(ACCESS_B, source_b.id))

    await _set_scope(session, ACCESS_B)
    assert (await session.scalars(select(kind.model.id))).all() == [record_b.id]
    assert (
        await session.scalars(select(kind.provenance_model.source_capture_event_id))
    ).all() == [source_b.id]
    assert await session.scalar(select(func.count()).select_from(kind.model)) == 1
    assert (
        await session.scalar(select(func.count()).select_from(kind.provenance_model))
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", RECORD_KINDS)
async def test_knowledge_records_and_provenance_are_hidden_without_scope(
    kind: RecordKind, engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))
    source = await capture_repository.create(capture_command(ACCESS_A, 110))
    record = await kind.repository_factory(engine).create(
        kind.command_factory(ACCESS_A, source.id)
    )

    assert (
        await session.scalar(select(kind.model.id).where(kind.model.id == record.id))
        is None
    )
    assert await session.scalar(select(func.count()).select_from(kind.model)) == 0
    assert (
        await session.scalar(select(func.count()).select_from(kind.provenance_model))
        == 0
    )
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(kind.model).values(
                id=uuid4(),
                user_space_id=ACCESS_A.user_space_id,
                text="a",
                source_capture_event_id=source.id,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(kind.provenance_model).values(
                **{
                    f"{kind.model.__tablename__.removesuffix('s')}_id": record.id,
                    "source_capture_event_id": source.id,
                    "user_space_id": ACCESS_A.user_space_id,
                    "created_at": NOW,
                    "trace_id": "1" * 32,
                }
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", RECORD_KINDS)
async def test_foreign_user_space_cannot_insert_knowledge_or_provenance(
    kind: RecordKind, engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))
    source_b = await capture_repository.create(capture_command(ACCESS_B, 120))
    record_b = await kind.repository_factory(engine).create(
        kind.command_factory(ACCESS_B, source_b.id)
    )
    await _set_scope(session, ACCESS_A)

    with pytest.raises(DBAPIError):
        await session.execute(
            insert(kind.model).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                text="b",
                source_capture_event_id=source_b.id,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )
    await session.rollback()
    await _set_scope(session, ACCESS_A)
    with pytest.raises(DBAPIError):
        await session.execute(
            insert(kind.provenance_model).values(
                **{
                    f"{kind.model.__tablename__.removesuffix('s')}_id": record_b.id,
                    "source_capture_event_id": source_b.id,
                    "user_space_id": ACCESS_B.user_space_id,
                    "created_at": NOW,
                    "trace_id": "1" * 32,
                }
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", RECORD_KINDS)
async def test_knowledge_record_requires_source_from_its_own_user_space(
    kind: RecordKind, engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))
    source_a = await capture_repository.create(capture_command(ACCESS_A, 130))
    await _set_scope(session, ACCESS_B)

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(kind.model).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                text="b",
                source_capture_event_id=source_a.id,
                created_at=NOW,
                updated_at=NOW,
                trace_id="1" * 32,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", RECORD_KINDS)
async def test_knowledge_provenance_requires_source_from_its_own_user_space(
    kind: RecordKind, engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_repository = PostgresCaptureEventRepository(create_session_factory(engine))
    source_a = await capture_repository.create(capture_command(ACCESS_A, 140))
    source_b = await capture_repository.create(capture_command(ACCESS_B, 141))
    record_b = await kind.repository_factory(engine).create(
        kind.command_factory(ACCESS_B, source_b.id)
    )
    await _set_scope(session, ACCESS_B)

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(kind.provenance_model).values(
                **{
                    f"{kind.model.__tablename__.removesuffix('s')}_id": record_b.id,
                    "source_capture_event_id": source_a.id,
                    "user_space_id": ACCESS_B.user_space_id,
                    "created_at": NOW,
                    "trace_id": "1" * 32,
                }
            )
        )


@pytest.mark.asyncio
async def test_knowledge_tables_have_forced_row_level_security(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    table_names = [
        "notes",
        "note_provenance",
        "ideas",
        "idea_provenance",
        "decisions",
        "decision_provenance",
        "questions",
        "question_provenance",
    ]
    async with schema_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class JOIN pg_namespace ON pg_namespace.oid = relnamespace "
                "WHERE relname = ANY(:table_names) AND nspname = :schema "
                "ORDER BY relname"
            ),
            {"table_names": table_names, "schema": isolated_database.schema},
        )

    assert result.all() == [
        ("decision_provenance", True, True),
        ("decisions", True, True),
        ("idea_provenance", True, True),
        ("ideas", True, True),
        ("note_provenance", True, True),
        ("notes", True, True),
        ("question_provenance", True, True),
        ("questions", True, True),
    ]


async def _set_scope(session: AsyncSession, access_context: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

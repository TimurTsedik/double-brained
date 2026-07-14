import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CreateTextProcessingRunCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingRun,
    TranscriptionOutputType,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
    SemanticDocumentModel,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
    INDEX_VERSION,
    RegisterIndexingTargetCommand,
    StoreSemanticChunksCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    IndexedChunk,
    IndexingTarget,
    SearchRecordType,
    SemanticMatch,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
TRACE_ID = "1" * 32
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_semantic_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [user_row(ACCESS_A), user_row(ACCESS_B)])
        await connection.execute(
            insert(UserSpace), [space_row(ACCESS_A), space_row(ACCESS_B)]
        )


def user_row(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_id,
        "role": "admin",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def space_row(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_space_id,
        "owner_user_id": access.user_id,
        "timezone": "Asia/Jerusalem",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def vector_of(*head: float) -> tuple[float, ...]:
    return head + (0.0,) * (EMBEDDING_DIMENSIONS - len(head))


def make_chunk(
    chunk_number: int, content: str, embedding: tuple[float, ...]
) -> IndexedChunk:
    return IndexedChunk(
        chunk_number=chunk_number,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        text=content,
        embedding=embedding,
    )


def chunks_command(
    access: AccessContext,
    *,
    record_id: UUID,
    capture_event_id: UUID,
    chunks: tuple[IndexedChunk, ...],
    record_kind: SearchRecordType = SearchRecordType.NOTE,
    embedding_model: str = EMBEDDING_MODEL_NAME,
    index_version: int = INDEX_VERSION,
) -> StoreSemanticChunksCommand:
    return StoreSemanticChunksCommand(
        access_context=access,
        record_kind=record_kind,
        record_id=record_id,
        source_capture_event_id=capture_event_id,
        chunks=chunks,
        embedding_model=embedding_model,
        index_version=index_version,
        created_at=NOW,
        trace_id=TRACE_ID,
    )


async def add_capture(schema_engine: AsyncEngine, access: AccessContext) -> UUID:
    capture_event_id = uuid4()
    update_id = int(capture_event_id.int % 9_000_000_000) + 1
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_event_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 10_000_000_000,
                raw_text="captured text",
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
    return capture_event_id


async def create_text_run(
    engine: AsyncEngine, schema_engine: AsyncEngine, access: AccessContext
) -> ProcessingRun:
    capture_event_id = await add_capture(schema_engine, access)
    repository = PostgresProcessingRepository(create_session_factory(engine))
    return await repository.create_text_run(
        CreateTextProcessingRunCommand(
            access_context=access,
            capture_event_id=capture_event_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=TRACE_ID,
        )
    )


async def store_chunks(
    engine: AsyncEngine, command: StoreSemanticChunksCommand
) -> None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresSemanticIndexWriter(session).insert_chunks(command)


async def register_target(
    engine: AsyncEngine, command: RegisterIndexingTargetCommand
) -> None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresSemanticIndexWriter(session).register_target(command)


async def read_target(
    engine: AsyncEngine, access: AccessContext, processing_run_id: UUID
) -> IndexingTarget | None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await PostgresSemanticIndexWriter(session).read_target(
                access, processing_run_id
            )


async def existing_chunks(
    engine: AsyncEngine,
    access: AccessContext,
    record_kind: SearchRecordType,
    record_id: UUID,
    index_version: int,
) -> tuple[tuple[int, str], ...]:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await PostgresSemanticIndexWriter(session).existing_chunks(
                access, record_kind, record_id, index_version
            )


async def search_similar(
    engine: AsyncEngine,
    access: AccessContext,
    query_vector: tuple[float, ...],
    limit: int,
) -> tuple[SemanticMatch, ...]:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await PostgresSemanticIndexWriter(session).search_similar(
                access, query_vector, limit
            )


async def scope_to(session: AsyncSession, access: AccessContext) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :value, true)"),
        {"value": str(access.user_space_id)},
    )


@pytest.mark.asyncio
async def test_chunk_batch_round_trips_vectors_and_repeat_insert_is_a_noop(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_event_id = await add_capture(schema_engine, ACCESS_A)
    record_id = uuid4()
    chunks = (
        make_chunk(0, "first chunk", vector_of(1.0, -0.5)),
        make_chunk(1, "second chunk", vector_of(0.25, 0.5, 0.75)),
    )
    command = chunks_command(
        ACCESS_A,
        record_id=record_id,
        capture_event_id=capture_event_id,
        chunks=chunks,
    )

    await store_chunks(engine, command)
    await store_chunks(engine, command)

    await scope_to(session, ACCESS_A)
    rows = (
        (
            await session.execute(
                select(SemanticDocumentModel).order_by(
                    SemanticDocumentModel.chunk_number
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    for row, chunk in zip(rows, chunks, strict=True):
        embedding = tuple(float(value) for value in row.embedding)
        assert len(embedding) == EMBEDDING_DIMENSIONS
        assert embedding == chunk.embedding
        assert row.chunk_number == chunk.chunk_number
        assert row.content_sha256 == chunk.content_sha256
        assert row.chunk_text == chunk.text
        assert row.source_kind is SearchRecordType.NOTE
        assert row.source_record_id == record_id
        assert row.source_capture_event_id == capture_event_id
        assert row.embedding_model == EMBEDDING_MODEL_NAME
        assert row.index_version == INDEX_VERSION


@pytest.mark.asyncio
async def test_insert_chunks_with_empty_batch_is_a_silent_noop(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    capture_event_id = await add_capture(schema_engine, ACCESS_A)

    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=uuid4(),
            capture_event_id=capture_event_id,
            chunks=(),
        ),
    )

    await scope_to(session, ACCESS_A)
    assert (
        await session.scalar(select(func.count()).select_from(SemanticDocumentModel))
        == 0
    )


@pytest.mark.asyncio
async def test_existing_chunks_returns_own_space_pairs_for_one_index_version(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    record_id = uuid4()
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    chunks_a = (
        make_chunk(0, "a zero", vector_of(1.0)),
        make_chunk(1, "a one", vector_of(0.5)),
    )
    chunk_b = make_chunk(0, "b zero", vector_of(-1.0))
    stale_chunk = make_chunk(0, "a stale version", vector_of(0.25))
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=record_id,
            capture_event_id=capture_a,
            chunks=chunks_a,
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_B,
            record_id=record_id,
            capture_event_id=capture_b,
            chunks=(chunk_b,),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=record_id,
            capture_event_id=capture_a,
            chunks=(stale_chunk,),
            index_version=INDEX_VERSION + 1,
        ),
    )

    pairs_a = await existing_chunks(
        engine, ACCESS_A, SearchRecordType.NOTE, record_id, INDEX_VERSION
    )
    pairs_b = await existing_chunks(
        engine, ACCESS_B, SearchRecordType.NOTE, record_id, INDEX_VERSION
    )

    assert pairs_a == (
        (0, chunks_a[0].content_sha256),
        (1, chunks_a[1].content_sha256),
    )
    assert pairs_b == ((0, chunk_b.content_sha256),)


@pytest.mark.asyncio
async def test_owner_connection_cannot_insert_chunk_with_foreign_capture_event(
    schema_engine: AsyncEngine,
) -> None:
    capture_b = await add_capture(schema_engine, ACCESS_B)
    chunk = make_chunk(0, "cross space", vector_of(1.0))

    async with create_session_factory(schema_engine)() as owner_session:
        with pytest.raises(IntegrityError):
            await owner_session.execute(
                insert(SemanticDocumentModel).values(
                    id=uuid4(),
                    user_space_id=ACCESS_A.user_space_id,
                    source_kind=SearchRecordType.NOTE,
                    source_record_id=uuid4(),
                    source_capture_event_id=capture_b,
                    chunk_number=chunk.chunk_number,
                    content_sha256=chunk.content_sha256,
                    chunk_text=chunk.text,
                    embedding_model=EMBEDDING_MODEL_NAME,
                    index_version=INDEX_VERSION,
                    embedding=list(chunk.embedding),
                    created_at=NOW,
                    trace_id=TRACE_ID,
                )
            )


@pytest.mark.asyncio
async def test_owner_connection_cannot_register_target_for_foreign_run(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    run_b = await create_text_run(engine, schema_engine, ACCESS_B)

    async with create_session_factory(schema_engine)() as owner_session:
        with pytest.raises(IntegrityError):
            await owner_session.execute(
                insert(IndexingTargetModel).values(
                    processing_run_id=run_b.id,
                    user_space_id=ACCESS_A.user_space_id,
                    record_kind=SearchRecordType.NOTE,
                    record_id=uuid4(),
                    created_at=NOW,
                    trace_id=TRACE_ID,
                )
            )


@pytest.mark.asyncio
async def test_register_target_is_idempotent_and_never_overwrites_the_target(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    run = await create_text_run(engine, schema_engine, ACCESS_A)
    first_record_id = uuid4()
    first = RegisterIndexingTargetCommand(
        access_context=ACCESS_A,
        processing_run_id=run.id,
        record_kind=SearchRecordType.NOTE,
        record_id=first_record_id,
        created_at=NOW,
        trace_id=TRACE_ID,
    )

    await register_target(engine, first)
    await register_target(engine, first)
    await register_target(
        engine,
        RegisterIndexingTargetCommand(
            access_context=ACCESS_A,
            processing_run_id=run.id,
            record_kind=SearchRecordType.TASK,
            record_id=uuid4(),
            created_at=NOW,
            trace_id="2" * 32,
        ),
    )

    target = await read_target(engine, ACCESS_A, run.id)
    assert target == IndexingTarget(
        record_kind=SearchRecordType.NOTE,
        record_id=first_record_id,
        capture_event_id=run.capture_event_id,
    )
    await scope_to(session, ACCESS_A)
    assert (
        await session.scalar(select(func.count()).select_from(IndexingTargetModel)) == 1
    )


@pytest.mark.asyncio
async def test_read_target_requires_the_owning_access_context(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    run = await create_text_run(engine, schema_engine, ACCESS_A)
    await register_target(
        engine,
        RegisterIndexingTargetCommand(
            access_context=ACCESS_A,
            processing_run_id=run.id,
            record_kind=SearchRecordType.IDEA,
            record_id=uuid4(),
            created_at=NOW,
            trace_id=TRACE_ID,
        ),
    )

    assert await read_target(engine, ACCESS_B, run.id) is None
    assert await read_target(engine, ACCESS_A, uuid4()) is None
    target = await read_target(engine, ACCESS_A, run.id)
    assert target is not None
    assert target.capture_event_id == run.capture_event_id


@pytest.mark.asyncio
async def test_search_similar_orders_by_cosine_distance_with_deterministic_ties(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_event_id = await add_capture(schema_engine, ACCESS_A)
    first_record = UUID("00000000-0000-0000-0000-0000000000a1")
    second_record = UUID("00000000-0000-0000-0000-0000000000a2")
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=first_record,
            capture_event_id=capture_event_id,
            chunks=(
                make_chunk(0, "closest first record", vector_of(1.0)),
                make_chunk(1, "orthogonal chunk", vector_of(0.0, 1.0)),
            ),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=second_record,
            capture_event_id=capture_event_id,
            chunks=(make_chunk(0, "closest second record", vector_of(1.0)),),
        ),
    )

    matches = await search_similar(engine, ACCESS_A, vector_of(1.0), limit=10)

    assert [(match.record_id, match.chunk_number, match.text) for match in matches] == [
        (first_record, 0, "closest first record"),
        (second_record, 0, "closest second record"),
        (first_record, 1, "orthogonal chunk"),
    ]
    assert all(match.record_kind is SearchRecordType.NOTE for match in matches)
    assert all(match.source_capture_event_id == capture_event_id for match in matches)
    assert all(match.created_at == NOW for match in matches)

    limited = await search_similar(engine, ACCESS_A, vector_of(1.0), limit=2)
    assert [(match.record_id, match.chunk_number) for match in limited] == [
        (first_record, 0),
        (second_record, 0),
    ]


@pytest.mark.asyncio
async def test_search_similar_filters_stale_versions_and_models_before_ranking(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_event_id = await add_capture(schema_engine, ACCESS_A)
    current_record = uuid4()
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=current_record,
            capture_event_id=capture_event_id,
            chunks=(make_chunk(0, "current but farther", vector_of(0.6, 0.8)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=uuid4(),
            capture_event_id=capture_event_id,
            chunks=(make_chunk(0, "stale version exact match", vector_of(1.0)),),
            index_version=INDEX_VERSION + 1,
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=uuid4(),
            capture_event_id=capture_event_id,
            chunks=(make_chunk(0, "other model exact match", vector_of(1.0)),),
            embedding_model="other/embedding-model",
        ),
    )

    matches = await search_similar(engine, ACCESS_A, vector_of(1.0), limit=1)

    assert [(match.record_id, match.text) for match in matches] == [
        (current_record, "current but farther")
    ]


@pytest.mark.asyncio
async def test_semantic_tables_have_no_hnsw_or_ivfflat_indexes(
    session: AsyncSession,
) -> None:
    definitions = (
        await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND tablename IN ('semantic_documents', 'indexing_targets')"
            )
        )
    ).scalars()

    for definition in definitions:
        assert "hnsw" not in definition.lower()
        assert "ivfflat" not in definition.lower()


def test_semantic_index_contracts_hide_content_and_identifiers_in_repr() -> None:
    record_id = uuid4()
    capture_event_id = uuid4()
    processing_run_id = uuid4()
    chunk = make_chunk(0, "secret chunk text", vector_of(1.0))
    command = chunks_command(
        ACCESS_A,
        record_id=record_id,
        capture_event_id=capture_event_id,
        chunks=(chunk,),
    )
    register = RegisterIndexingTargetCommand(
        access_context=ACCESS_A,
        processing_run_id=processing_run_id,
        record_kind=SearchRecordType.NOTE,
        record_id=record_id,
        created_at=NOW,
        trace_id=TRACE_ID,
    )
    target = IndexingTarget(
        record_kind=SearchRecordType.NOTE,
        record_id=record_id,
        capture_event_id=capture_event_id,
    )
    match = SemanticMatch(
        record_kind=SearchRecordType.NOTE,
        record_id=record_id,
        source_capture_event_id=capture_event_id,
        chunk_number=0,
        text="secret chunk text",
        created_at=NOW,
    )

    hidden = (
        "secret",
        str(record_id),
        str(capture_event_id),
        str(processing_run_id),
        str(ACCESS_A.user_id),
        str(ACCESS_A.user_space_id),
    )
    for rendered in (repr(command), repr(register), repr(target), repr(match)):
        for fragment in hidden:
            assert fragment not in rendered

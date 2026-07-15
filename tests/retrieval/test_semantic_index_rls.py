from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
    SemanticDocumentModel,
)
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.test_semantic_index_persistence import (
    ACCESS_A,
    ACCESS_B,
    NOW,
    TRACE_ID,
    add_capture,
    chunks_command,
    create_text_run,
    make_chunk,
    register_target,
    scope_to,
    search_similar,
    space_row,
    store_chunks,
    user_row,
    vector_of,
)

SEMANTIC_TABLES = ("semantic_documents", "indexing_targets")


@pytest_asyncio.fixture(autouse=True)
async def reset_semantic_rls_schema(
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


@pytest.mark.asyncio
async def test_forced_rls_hides_foreign_chunks_and_indexing_targets(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    record_a = uuid4()
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=record_a,
            capture_event_id=capture_a,
            chunks=(make_chunk(0, "a private", vector_of(1.0)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_B,
            record_id=uuid4(),
            capture_event_id=capture_b,
            chunks=(
                make_chunk(0, "b private zero", vector_of(0.5)),
                make_chunk(1, "b private one", vector_of(0.25)),
            ),
        ),
    )
    run_a = await create_text_run(engine, schema_engine, ACCESS_A)
    run_b = await create_text_run(engine, schema_engine, ACCESS_B)
    for access, run in ((ACCESS_A, run_a), (ACCESS_B, run_b)):
        await register_target(
            engine,
            RegisterIndexingTargetCommand(
                access_context=access,
                processing_run_id=run.id,
                record_kind=SearchRecordType.NOTE,
                record_id=uuid4(),
                created_at=NOW,
                trace_id=TRACE_ID,
            ),
        )

    await scope_to(session, ACCESS_A)

    assert (
        await session.scalars(select(SemanticDocumentModel.source_record_id))
    ).all() == [record_a]
    assert (
        await session.scalar(select(func.count()).select_from(SemanticDocumentModel))
        == 1
    )
    assert (
        await session.scalars(select(IndexingTargetModel.processing_run_id))
    ).all() == [run_a.id]


@pytest.mark.asyncio
async def test_search_similar_never_surfaces_closer_foreign_rows(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    record_a = uuid4()
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    query = vector_of(1.0)
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=record_a,
            capture_event_id=capture_a,
            chunks=(make_chunk(0, "a farther", vector_of(0.6, 0.8)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_B,
            record_id=uuid4(),
            capture_event_id=capture_b,
            chunks=(make_chunk(0, "b exact match", query),),
        ),
    )

    matches = await search_similar(engine, ACCESS_A, query, limit=10)

    assert [(match.record_id, match.text) for match in matches] == [
        (record_a, "a farther")
    ]


@pytest.mark.asyncio
async def test_search_similar_hides_closer_admin_rows_from_member(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Реципрокно: member (B) поиском по вектору не видит даже более БЛИЗКИХ строк
    # admin'а (A). Приватность в обе стороны, admin НЕ суперпользователь.
    record_b = uuid4()
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    query = vector_of(1.0)
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=uuid4(),
            capture_event_id=capture_a,
            chunks=(make_chunk(0, "a exact match", query),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_B,
            record_id=record_b,
            capture_event_id=capture_b,
            chunks=(make_chunk(0, "b farther", vector_of(0.6, 0.8)),),
        ),
    )

    matches = await search_similar(engine, ACCESS_B, query, limit=10)

    assert [(match.record_id, match.text) for match in matches] == [
        (record_b, "b farther")
    ]


@pytest.mark.asyncio
async def test_semantic_tables_enable_and_force_row_level_security(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    async with schema_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relname = ANY(:tables)"
                ),
                {
                    "schema": isolated_database.schema,
                    "tables": list(SEMANTIC_TABLES),
                },
            )
        ).all()

    assert {row[0]: (row[1], row[2]) for row in rows} == {
        table: (True, True) for table in SEMANTIC_TABLES
    }

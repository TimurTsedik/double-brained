"""Показ записи целиком + «похожее по смыслу» (retrieval-слой, живой PostgreSQL).

Чтение записи идёт СТРОГО по тройке (тип, uuid, пространство вызывающего) под
RLS — тип из callback'а не доверенный, id-таблицы независимы и uuid-коллизии
между ними возможны. Родственные считаются только по чанкам ТЕКУЩИХ
embedding_model+INDEX_VERSION: минимальная дистанция по всем своим чанкам,
дедуп до записей, join обратно в каноническую таблицу, сама запись исключена.
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresRecordViewReader,
)
from second_brain.slices.retrieval.application.contracts import INDEX_VERSION
from second_brain.slices.retrieval.application.record_view import ShowRecord
from second_brain.slices.retrieval.domain.entities import (
    RecordView,
    SearchRecordType,
)
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.tasks.domain.entities import TaskStatus
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.test_semantic_index_persistence import (
    ACCESS_A,
    ACCESS_B,
    NOW,
    TRACE_ID,
    add_capture,
    chunks_command,
    make_chunk,
    space_row,
    store_chunks,
    user_row,
    vector_of,
)


@pytest_asyncio.fixture(autouse=True)
async def reset_record_view_schema(
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


async def add_note(
    schema_engine: AsyncEngine,
    access: AccessContext,
    capture_event_id: UUID,
    text: str,
    note_id: UUID | None = None,
) -> UUID:
    note_id = note_id or uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(NoteModel).values(
                id=note_id,
                user_space_id=access.user_space_id,
                text=text,
                source_capture_event_id=capture_event_id,
                created_at=NOW,
                updated_at=NOW,
                trace_id=TRACE_ID,
            )
        )
    return note_id


async def add_task(
    schema_engine: AsyncEngine,
    access: AccessContext,
    capture_event_id: UUID,
    title: str,
    status: TaskStatus,
    task_id: UUID | None = None,
) -> UUID:
    task_id = task_id or uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(TaskModel).values(
                id=task_id,
                user_space_id=access.user_space_id,
                title=title,
                description=None,
                status=status,
                source_capture_event_id=capture_event_id,
                created_at=NOW,
                updated_at=NOW,
                trace_id=TRACE_ID,
            )
        )
    return task_id


async def read_record(
    engine: AsyncEngine,
    access: AccessContext,
    record_kind: SearchRecordType,
    record_id: UUID,
) -> RecordView | None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await ShowRecord(PostgresRecordViewReader(session)).read_record_full(
                access, record_kind, record_id
            )


async def related_records(
    engine: AsyncEngine,
    access: AccessContext,
    record_kind: SearchRecordType,
    record_id: UUID,
) -> tuple[RecordView, ...]:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await ShowRecord(PostgresRecordViewReader(session)).related_records(
                access, record_kind, record_id
            )


@pytest.mark.asyncio
async def test_read_record_full_returns_typed_text_and_completed_flag(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    note_id = await add_note(schema_engine, ACCESS_A, capture, "полный текст заметки")
    open_task_id = await add_task(
        schema_engine, ACCESS_A, capture, "открытая задача", TaskStatus.INBOX
    )
    done_task_id = await add_task(
        schema_engine, ACCESS_A, capture, "закрытая задача", TaskStatus.COMPLETED
    )

    note = await read_record(engine, ACCESS_A, SearchRecordType.NOTE, note_id)
    open_task = await read_record(engine, ACCESS_A, SearchRecordType.TASK, open_task_id)
    done_task = await read_record(engine, ACCESS_A, SearchRecordType.TASK, done_task_id)

    assert note == RecordView(
        id=note_id,
        record_type=SearchRecordType.NOTE,
        text="полный текст заметки",
        created_at=NOW,
        task_completed=None,
    )
    assert open_task is not None
    assert (open_task.text, open_task.task_completed) == ("открытая задача", False)
    assert done_task is not None
    assert (done_task.text, done_task.task_completed) == ("закрытая задача", True)


@pytest.mark.asyncio
async def test_read_record_maps_a_colliding_uuid_by_its_type(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Форс-коллизия: ОДИН uuid в notes и tasks одновременно — маппинг по типу
    # обязан быть точным, «по uuid без типа» читать нельзя.
    capture = await add_capture(schema_engine, ACCESS_A)
    collision = uuid4()
    await add_note(schema_engine, ACCESS_A, capture, "текст заметки", collision)
    await add_task(
        schema_engine,
        ACCESS_A,
        capture,
        "заголовок задачи",
        TaskStatus.INBOX,
        collision,
    )

    note = await read_record(engine, ACCESS_A, SearchRecordType.NOTE, collision)
    task = await read_record(engine, ACCESS_A, SearchRecordType.TASK, collision)
    idea = await read_record(engine, ACCESS_A, SearchRecordType.IDEA, collision)

    assert note is not None and note.text == "текст заметки"
    assert task is not None and task.text == "заголовок задачи"
    assert idea is None


@pytest.mark.asyncio
async def test_read_record_hides_foreign_records_in_both_directions(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    note_a = await add_note(schema_engine, ACCESS_A, capture_a, "приватно A")
    note_b = await add_note(schema_engine, ACCESS_B, capture_b, "приватно B")

    assert await read_record(engine, ACCESS_A, SearchRecordType.NOTE, note_b) is None
    assert await read_record(engine, ACCESS_B, SearchRecordType.NOTE, note_a) is None
    assert await read_record(engine, ACCESS_A, SearchRecordType.NOTE, uuid4()) is None


@pytest.mark.asyncio
async def test_related_ranks_by_min_distance_across_own_chunks_and_dedupes(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    shown_id = await add_note(schema_engine, ACCESS_A, capture, "показанная запись")
    nearest_id = await add_note(schema_engine, ACCESS_A, capture, "ближайшая")
    middle_id = await add_note(schema_engine, ACCESS_A, capture, "средняя")
    far_id = await add_note(schema_engine, ACCESS_A, capture, "дальняя")
    farthest_id = await add_note(schema_engine, ACCESS_A, capture, "за лимитом")
    # У показанной записи ДВА чанка — дистанция кандидата считается как минимум
    # по всем своим чанкам.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=shown_id,
            capture_event_id=capture,
            chunks=(
                make_chunk(0, "shown zero", vector_of(1.0)),
                make_chunk(1, "shown one", vector_of(0.0, 1.0)),
            ),
        ),
    )
    # nearest: два чанка, лучший — точное совпадение (dist 0); дедуп до записи.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=nearest_id,
            capture_event_id=capture,
            chunks=(
                make_chunk(0, "nearest exact", vector_of(1.0)),
                make_chunk(1, "nearest other", vector_of(0.6, 0.8)),
            ),
        ),
    )
    # middle: min(0.4 к первому чанку, 0.2 ко второму) = 0.2.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=middle_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "middle", vector_of(0.6, 0.8)),),
        ),
    )
    # far: ортогонален обоим чанкам (dist 1).
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=far_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "far", vector_of(0.0, 0.0, 1.0)),),
        ),
    )
    # farthest: противоположный вектор (dist 2) — не влезает в топ-3.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=farthest_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "farthest", vector_of(-1.0)),),
        ),
    )

    related = await related_records(engine, ACCESS_A, SearchRecordType.NOTE, shown_id)

    assert [(record.id, record.text) for record in related] == [
        (nearest_id, "ближайшая"),
        (middle_id, "средняя"),
        (far_id, "дальняя"),
    ]


@pytest.mark.asyncio
async def test_related_uses_only_current_model_and_version_chunks(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    shown_id = await add_note(schema_engine, ACCESS_A, capture, "показанная запись")
    stale_id = await add_note(schema_engine, ACCESS_A, capture, "стейл-версия")
    other_model_id = await add_note(schema_engine, ACCESS_A, capture, "другая модель")
    current_id = await add_note(schema_engine, ACCESS_A, capture, "текущая")
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=shown_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "shown", vector_of(1.0)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=stale_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "stale exact", vector_of(1.0)),),
            index_version=INDEX_VERSION + 1,
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=other_model_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "other model exact", vector_of(1.0)),),
            embedding_model="other/embedding-model",
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=current_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "current farther", vector_of(0.6, 0.8)),),
        ),
    )

    related = await related_records(engine, ACCESS_A, SearchRecordType.NOTE, shown_id)

    assert [record.id for record in related] == [current_id]


@pytest.mark.asyncio
async def test_related_is_empty_for_an_unindexed_record(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    unindexed_id = await add_note(schema_engine, ACCESS_A, capture, "без вектора")
    stale_only_id = await add_note(schema_engine, ACCESS_A, capture, "только стейл")
    neighbour_id = await add_note(schema_engine, ACCESS_A, capture, "сосед")
    # Сосед проиндексирован, но у самой записи нет чанков ТЕКУЩИХ model+version
    # (совсем нет или только stale) → секции нет.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=neighbour_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "neighbour", vector_of(1.0)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=stale_only_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "stale own", vector_of(1.0)),),
            index_version=INDEX_VERSION + 1,
        ),
    )

    assert (
        await related_records(engine, ACCESS_A, SearchRecordType.NOTE, unindexed_id)
        == ()
    )
    assert (
        await related_records(engine, ACCESS_A, SearchRecordType.NOTE, stale_only_id)
        == ()
    )


@pytest.mark.asyncio
async def test_related_never_mixes_spaces_bidirectionally(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    shown_a = await add_note(schema_engine, ACCESS_A, capture_a, "запись A")
    own_a = await add_note(schema_engine, ACCESS_A, capture_a, "сосед A")
    shown_b = await add_note(schema_engine, ACCESS_B, capture_b, "запись B")
    own_b = await add_note(schema_engine, ACCESS_B, capture_b, "сосед B")
    for access, capture, shown, _own in (
        (ACCESS_A, capture_a, shown_a, own_a),
        (ACCESS_B, capture_b, shown_b, own_b),
    ):
        await store_chunks(
            engine,
            chunks_command(
                access,
                record_id=shown,
                capture_event_id=capture,
                chunks=(make_chunk(0, "shown", vector_of(1.0)),),
            ),
        )
    # Чужой сосед — ТОЧНОЕ совпадение, свой — дальше: чужой всё равно не виден.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=own_a,
            capture_event_id=capture_a,
            chunks=(make_chunk(0, "own a farther", vector_of(0.6, 0.8)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_B,
            record_id=own_b,
            capture_event_id=capture_b,
            chunks=(make_chunk(0, "own b exact", vector_of(1.0)),),
        ),
    )

    related_a = await related_records(engine, ACCESS_A, SearchRecordType.NOTE, shown_a)
    related_b = await related_records(engine, ACCESS_B, SearchRecordType.NOTE, shown_b)

    assert [record.id for record in related_a] == [own_a]
    assert [record.id for record in related_b] == [own_b]


@pytest.mark.asyncio
async def test_related_drops_candidates_without_a_canonical_row(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    shown_id = await add_note(schema_engine, ACCESS_A, capture, "показанная запись")
    real_id = await add_note(schema_engine, ACCESS_A, capture, "настоящая")
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=shown_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "shown", vector_of(1.0)),),
        ),
    )
    # Осиротевший чанк: канонической строки нет — кандидат просто выпадает.
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=uuid4(),
            capture_event_id=capture,
            chunks=(make_chunk(0, "orphan exact", vector_of(1.0)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=real_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "real farther", vector_of(0.6, 0.8)),),
        ),
    )

    related = await related_records(engine, ACCESS_A, SearchRecordType.NOTE, shown_id)

    assert [record.id for record in related] == [real_id]


@pytest.mark.asyncio
async def test_related_includes_completed_tasks_with_the_completed_flag(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    shown_id = await add_note(schema_engine, ACCESS_A, capture, "показанная запись")
    done_task_id = await add_task(
        schema_engine, ACCESS_A, capture, "сделанная задача", TaskStatus.COMPLETED
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=shown_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "shown", vector_of(1.0)),),
        ),
    )
    await store_chunks(
        engine,
        chunks_command(
            ACCESS_A,
            record_id=done_task_id,
            capture_event_id=capture,
            chunks=(make_chunk(0, "done task", vector_of(1.0)),),
            record_kind=SearchRecordType.TASK,
        ),
    )

    related = await related_records(engine, ACCESS_A, SearchRecordType.NOTE, shown_id)

    assert [
        (record.id, record.record_type, record.task_completed) for record in related
    ] == [(done_task_id, SearchRecordType.TASK, True)]

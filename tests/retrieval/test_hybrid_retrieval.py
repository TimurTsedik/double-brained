import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.domain.entities import ReminderStatus
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresExactSearchWriter,
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_MODEL_NAME,
    INDEX_VERSION,
    RetrieveMemoryCommand,
    StoreSemanticChunksCommand,
)
from second_brain.slices.retrieval.application.hybrid_retrieval import (
    FTS_CANDIDATES,
    MAX_CHARS,
    MAX_CHUNKS,
    RRF_K,
    VECTOR_CANDIDATES,
    HybridMemoryRetrieval,
)
from second_brain.slices.retrieval.domain.entities import (
    EvidenceBundle,
    EvidenceChunk,
    IndexedChunk,
    MatchQuality,
    SearchRecord,
    SearchRecordType,
    SemanticMatch,
)
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.tasks.domain.entities import TaskStatus
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.embedding_fakes import FakeEmbeddingModel

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
TRACE_ID = "1" * 32
QUESTION = "как устроен гибридный поиск"
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


class RecordingFtsStore:
    def __init__(self, records: tuple[SearchRecord, ...] = ()) -> None:
        self.records = records
        self.calls: list[tuple[AccessContext, str, int]] = []

    async def search(
        self, access_context: AccessContext, query: str, limit: int
    ) -> tuple[SearchRecord, ...]:
        self.calls.append((access_context, query, limit))
        return self.records[:limit]


class RecordingSemanticStore:
    def __init__(self, matches: tuple[SemanticMatch, ...] = ()) -> None:
        self.matches = matches
        self.calls: list[tuple[AccessContext, tuple[float, ...], int]] = []

    async def search_similar(
        self,
        access_context: AccessContext,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[SemanticMatch, ...]:
        self.calls.append((access_context, query_vector, limit))
        return self.matches[:limit]


def record_uuid(index: int) -> UUID:
    return UUID(f"a0000000-0000-0000-0000-{index:012d}")


def capture_uuid(index: int) -> UUID:
    return UUID(f"b0000000-0000-0000-0000-{index:012d}")


def fts_record(
    index: int,
    *,
    text: str = "",
    created_at: datetime = NOW,
    kind: SearchRecordType = SearchRecordType.NOTE,
    capture_index: int | None = None,
) -> SearchRecord:
    return SearchRecord(
        id=record_uuid(index),
        record_type=kind,
        text=text or f"fts-text-{index}",
        source_capture_event_id=capture_uuid(
            capture_index if capture_index is not None else index
        ),
        created_at=created_at,
        task_completed=None,
        match_quality=MatchQuality.FULL_TEXT,
    )


def vector_match(
    index: int,
    *,
    chunk_number: int = 0,
    text: str = "",
    created_at: datetime = NOW,
    kind: SearchRecordType = SearchRecordType.NOTE,
    capture_index: int | None = None,
) -> SemanticMatch:
    return SemanticMatch(
        record_kind=kind,
        record_id=record_uuid(index),
        source_capture_event_id=capture_uuid(
            capture_index if capture_index is not None else index
        ),
        chunk_number=chunk_number,
        text=text or f"vector-chunk-{index}-{chunk_number}",
        created_at=created_at,
    )


async def retrieve_in_memory(
    fts: tuple[SearchRecord, ...] = (),
    matches: tuple[SemanticMatch, ...] = (),
    *,
    question: str = QUESTION,
    current_project_id: UUID | None = None,
) -> tuple[
    EvidenceBundle, RecordingFtsStore, RecordingSemanticStore, FakeEmbeddingModel
]:
    fts_store = RecordingFtsStore(fts)
    semantic_store = RecordingSemanticStore(matches)
    model = FakeEmbeddingModel()
    bundle = await HybridMemoryRetrieval(fts_store, semantic_store, model).retrieve(
        RetrieveMemoryCommand(
            access_context=ACCESS_A,
            question=question,
            current_project_id=current_project_id,
        )
    )
    return bundle, fts_store, semantic_store, model


@pytest.mark.asyncio
async def test_two_runs_produce_byte_identical_bundles() -> None:
    fts = (fts_record(1), fts_record(2, created_at=NOW - timedelta(minutes=1)))
    matches = (
        vector_match(2, chunk_number=0),
        vector_match(3, chunk_number=0),
        vector_match(2, chunk_number=1),
    )

    first, *_ = await retrieve_in_memory(fts, matches)
    second, *_ = await retrieve_in_memory(fts, matches)

    assert first.chunks != ()
    assert first == second
    assert repr(first) == repr(second)


@pytest.mark.asyncio
async def test_stores_receive_normalized_question_and_candidate_limits() -> None:
    _, fts_store, semantic_store, model = await retrieve_in_memory(
        (fts_record(1),), (), question="  как   устроен поиск \n"
    )

    assert (FTS_CANDIDATES, VECTOR_CANDIDATES, RRF_K) == (24, 24, 60)
    assert fts_store.calls == [(ACCESS_A, "как устроен поиск", FTS_CANDIDATES)]
    assert model.query_calls == ["как устроен поиск"]
    assert model.document_calls == []
    assert len(semantic_store.calls) == 1
    access, query_vector, limit = semantic_store.calls[0]
    assert access == ACCESS_A
    assert limit == VECTOR_CANDIDATES
    assert query_vector == await FakeEmbeddingModel().embed_query("как устроен поиск")


@pytest.mark.asyncio
async def test_record_found_by_both_paths_outranks_single_path_record() -> None:
    # both (id=2): 1/62 + 1/62; fts-only (id=1): 1/61; vector-only (id=3): 1/61.
    fts = (fts_record(1), fts_record(2))
    matches = (vector_match(3), vector_match(2))

    bundle, *_ = await retrieve_in_memory(fts, matches)

    assert [(chunk.record_id, chunk.chunk_number) for chunk in bundle.chunks] == [
        (record_uuid(2), 0),
        (record_uuid(1), None),
        (record_uuid(3), 0),
    ]


@pytest.mark.asyncio
async def test_rrf_scores_use_exact_arithmetic_with_k_60() -> None:
    # id=1: 1/61 + 1/64 = 125/3904; id=2: 1/62 + 1/63 = 125/3906 — same
    # numerators, off-by-two denominators: id=1 is strictly above id=2.
    fts = (fts_record(1), fts_record(2))
    matches = (vector_match(5), vector_match(6), vector_match(2), vector_match(1))

    bundle, *_ = await retrieve_in_memory(fts, matches)

    assert [chunk.record_id for chunk in bundle.chunks] == [
        record_uuid(1),
        record_uuid(2),
        record_uuid(5),
        record_uuid(6),
    ]


@pytest.mark.asyncio
async def test_equal_scores_tie_break_deterministically_across_paths() -> None:
    # Rank 1 in either path scores exactly 1/61: newest created_at wins first.
    newer = NOW + timedelta(minutes=5)
    bundle, *_ = await retrieve_in_memory(
        (fts_record(1, created_at=NOW),), (vector_match(2, created_at=newer),)
    )
    assert [chunk.record_id for chunk in bundle.chunks] == [
        record_uuid(2),
        record_uuid(1),
    ]

    # Equal created_at: order comes from (kind, id) no matter which path
    # served which record — permuting the paths changes nothing.
    first, *_ = await retrieve_in_memory((fts_record(4),), (vector_match(3),))
    second, *_ = await retrieve_in_memory((fts_record(3),), (vector_match(4),))
    assert [chunk.record_id for chunk in first.chunks] == [
        record_uuid(3),
        record_uuid(4),
    ]
    assert [chunk.record_id for chunk in second.chunks] == [
        record_uuid(3),
        record_uuid(4),
    ]

    # Equal score and created_at with different kinds: kind sorts first.
    third, *_ = await retrieve_in_memory(
        (fts_record(7),), (vector_match(8, kind=SearchRecordType.DECISION),)
    )
    assert [(chunk.record_kind, chunk.record_id) for chunk in third.chunks] == [
        (SearchRecordType.DECISION, record_uuid(8)),
        (SearchRecordType.NOTE, record_uuid(7)),
    ]


@pytest.mark.asyncio
async def test_fts_hit_of_record_with_vector_chunks_adds_no_pseudo_chunk() -> None:
    fts = (fts_record(1, text="полный текст записи"),)
    matches = (
        vector_match(1, chunk_number=1, text="чанк один"),
        vector_match(1, chunk_number=0, text="чанк ноль"),
    )

    bundle, *_ = await retrieve_in_memory(fts, matches)

    # Only the record's own vector chunks, ordered by vector rank.
    assert [
        (chunk.record_id, chunk.chunk_number, chunk.text) for chunk in bundle.chunks
    ] == [
        (record_uuid(1), 1, "чанк один"),
        (record_uuid(1), 0, "чанк ноль"),
    ]


@pytest.mark.asyncio
async def test_source_sibling_chunks_with_same_text_are_emitted_once() -> None:
    shared = "общий текст сиблингов одного источника"
    matches = (
        vector_match(1, text=shared, capture_index=9),
        vector_match(2, kind=SearchRecordType.IDEA, text=shared, capture_index=9),
        vector_match(
            2,
            kind=SearchRecordType.IDEA,
            chunk_number=1,
            text="уникальный чанк",
            capture_index=9,
        ),
    )

    bundle, *_ = await retrieve_in_memory((), matches)

    # id=2 scores 1/62 + 1/63 and goes first; the sibling duplicate of the
    # shared text from id=1 (same capture event) is skipped.
    assert [
        (chunk.record_kind, chunk.record_id, chunk.chunk_number, chunk.text)
        for chunk in bundle.chunks
    ] == [
        (SearchRecordType.IDEA, record_uuid(2), 0, shared),
        (SearchRecordType.IDEA, record_uuid(2), 1, "уникальный чанк"),
    ]


@pytest.mark.asyncio
async def test_duplicate_text_chunks_of_one_record_are_emitted_once() -> None:
    matches = (
        vector_match(1, chunk_number=0, text="повтор"),
        vector_match(1, chunk_number=1, text="повтор"),
        vector_match(1, chunk_number=2, text="другое"),
    )

    bundle, *_ = await retrieve_in_memory((), matches)

    assert [(chunk.chunk_number, chunk.text) for chunk in bundle.chunks] == [
        (0, "повтор"),
        (2, "другое"),
    ]


@pytest.mark.asyncio
async def test_no_more_than_twelve_chunks_are_emitted() -> None:
    matches = tuple(vector_match(index) for index in range(1, 14))

    bundle, *_ = await retrieve_in_memory((), matches)

    assert MAX_CHUNKS == 12
    assert len(bundle.chunks) == 12
    assert record_uuid(13) not in {chunk.record_id for chunk in bundle.chunks}


@pytest.mark.asyncio
async def test_chunk_overflowing_total_characters_stops_emission() -> None:
    matches = (
        vector_match(1, text="а" * 6000),
        vector_match(2, text="б" * 5900),
        vector_match(3, text="в" * 200),
        vector_match(4, text="г"),
    )

    bundle, *_ = await retrieve_in_memory((), matches)

    # The third chunk would overflow MAX_CHARS: emission stops before it and
    # the later small chunk does not sneak in.
    assert MAX_CHARS == 12_000
    assert [chunk.record_id for chunk in bundle.chunks] == [
        record_uuid(1),
        record_uuid(2),
    ]
    assert sum(len(chunk.text) for chunk in bundle.chunks) == 11_900


@pytest.mark.asyncio
async def test_every_chunk_carries_source_provenance() -> None:
    fts = (fts_record(1, capture_index=21),)
    matches = (vector_match(2, chunk_number=3, capture_index=22),)

    bundle, *_ = await retrieve_in_memory(fts, matches)

    by_id = {chunk.record_id: chunk for chunk in bundle.chunks}
    vector_chunk = by_id[record_uuid(2)]
    assert vector_chunk.record_kind is SearchRecordType.NOTE
    assert vector_chunk.source_capture_event_id == capture_uuid(22)
    assert vector_chunk.chunk_number == 3
    assert vector_chunk.created_at == NOW
    pseudo_chunk = by_id[record_uuid(1)]
    assert pseudo_chunk.record_kind is SearchRecordType.NOTE
    assert pseudo_chunk.source_capture_event_id == capture_uuid(21)
    assert pseudo_chunk.chunk_number is None
    assert pseudo_chunk.text == "fts-text-1"


@pytest.mark.asyncio
async def test_current_project_id_is_passed_through_and_never_filters() -> None:
    fts = (fts_record(1),)
    matches = (vector_match(2),)
    project_id = uuid4()

    with_project, *_ = await retrieve_in_memory(
        fts, matches, current_project_id=project_id
    )
    without_project, *_ = await retrieve_in_memory(fts, matches)
    other_project, *_ = await retrieve_in_memory(
        fts, matches, current_project_id=uuid4()
    )

    assert with_project.current_project_id == project_id
    assert without_project.current_project_id is None
    assert with_project.chunks == without_project.chunks == other_project.chunks
    assert with_project.chunks != ()


@pytest.mark.asyncio
async def test_blank_question_returns_empty_bundle_without_store_calls() -> None:
    project_id = uuid4()

    bundle, fts_store, semantic_store, model = await retrieve_in_memory(
        (fts_record(1),),
        (vector_match(2),),
        question=" \n\t ",
        current_project_id=project_id,
    )

    assert bundle.chunks == ()
    assert bundle.current_project_id == project_id
    assert fts_store.calls == []
    assert semantic_store.calls == []
    assert model.query_calls == []
    assert model.document_calls == []


def test_retrieval_contracts_hide_content_and_identifiers_in_repr() -> None:
    project_id = uuid4()
    command = RetrieveMemoryCommand(
        access_context=ACCESS_A,
        question="секретный вопрос",
        current_project_id=project_id,
    )
    chunk = EvidenceChunk(
        record_kind=SearchRecordType.NOTE,
        record_id=record_uuid(1),
        source_capture_event_id=capture_uuid(1),
        chunk_number=0,
        text="секретный чанк",
        created_at=NOW,
    )
    bundle = EvidenceBundle(chunks=(chunk,), current_project_id=project_id)

    hidden = (
        "секретный",
        str(project_id),
        str(record_uuid(1)),
        str(capture_uuid(1)),
        str(ACCESS_A.user_id),
        str(ACCESS_A.user_space_id),
    )
    for rendered in (repr(command), repr(chunk), repr(bundle)):
        assert "UUID(" not in rendered
        for fragment in hidden:
            assert fragment not in rendered


@pytest_asyncio.fixture
async def seeded_spaces(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User), [_user_row(ACCESS_A), _user_row(ACCESS_B)]
        )
        await connection.execute(
            insert(UserSpace), [_space_row(ACCESS_A), _space_row(ACCESS_B)]
        )


def _user_row(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_id,
        "role": "member",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _space_row(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_space_id,
        "owner_user_id": access.user_id,
        "timezone": "Asia/Jerusalem",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


async def _add_note(
    schema_engine: AsyncEngine, access: AccessContext, content: str
) -> tuple[UUID, UUID]:
    source_id = uuid4()
    record_id = uuid4()
    update_id = int(source_id.int % 9_000_000_000) + 1
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=source_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 10_000_000_000,
                raw_text=content,
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
        await connection.execute(
            insert(NoteModel).values(
                id=record_id,
                user_space_id=access.user_space_id,
                source_capture_event_id=source_id,
                text=content,
                created_at=NOW,
                updated_at=NOW,
                trace_id=TRACE_ID,
            )
        )
    return record_id, source_id


async def _add_task(
    schema_engine: AsyncEngine,
    access: AccessContext,
    content: str,
    *,
    status: TaskStatus,
) -> tuple[UUID, UUID]:
    source_id = uuid4()
    record_id = uuid4()
    update_id = int(source_id.int % 9_000_000_000) + 1
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=source_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 10_000_000_000,
                raw_text=content,
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
        await connection.execute(
            insert(TaskModel).values(
                id=record_id,
                user_space_id=access.user_space_id,
                source_capture_event_id=source_id,
                title=content,
                description=None,
                status=status,
                created_at=NOW,
                updated_at=NOW,
                trace_id=TRACE_ID,
            )
        )
    return record_id, source_id


async def _add_reminder(
    schema_engine: AsyncEngine, access: AccessContext, source_task_id: UUID
) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(ReminderModel).values(
                id=uuid4(),
                user_space_id=access.user_space_id,
                remind_at=NOW,
                text="alarm reminder",
                status=ReminderStatus.SENT.value,
                source_task_id=source_task_id,
                send_attempts=0,
                next_attempt_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                trace_id=TRACE_ID,
            )
        )


def make_chunk(
    chunk_number: int, content: str, embedding: tuple[float, ...]
) -> IndexedChunk:
    return IndexedChunk(
        chunk_number=chunk_number,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        text=content,
        embedding=embedding,
    )


async def _store_chunks(
    engine: AsyncEngine,
    access: AccessContext,
    *,
    record_id: UUID,
    capture_event_id: UUID,
    chunks: tuple[IndexedChunk, ...],
    record_kind: SearchRecordType = SearchRecordType.NOTE,
) -> None:
    command = StoreSemanticChunksCommand(
        access_context=access,
        record_kind=record_kind,
        record_id=record_id,
        source_capture_event_id=capture_event_id,
        chunks=chunks,
        embedding_model=EMBEDDING_MODEL_NAME,
        index_version=INDEX_VERSION,
        created_at=NOW,
        trace_id=TRACE_ID,
    )
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresSemanticIndexWriter(session).insert_chunks(command)


async def question_vector(question: str) -> tuple[float, ...]:
    return await FakeEmbeddingModel().embed_query(question)


def near_vector(base: tuple[float, ...]) -> tuple[float, ...]:
    hot_index = base.index(1.0)
    vector = [0.0] * len(base)
    vector[hot_index] = 0.6
    vector[(hot_index + 1) % len(vector)] = 0.8
    return tuple(vector)


async def retrieve_from_postgres(
    engine: AsyncEngine,
    access: AccessContext,
    question: str,
    model: FakeEmbeddingModel,
) -> EvidenceBundle:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            retrieval = HybridMemoryRetrieval(
                PostgresExactSearchWriter(session),
                PostgresSemanticIndexWriter(session),
                model,
            )
            return await retrieval.retrieve(
                RetrieveMemoryCommand(access_context=access, question=question)
            )


@pytest.mark.asyncio
async def test_hybrid_finds_lexical_and_semantic_matches_without_duplicates(
    engine: AsyncEngine, schema_engine: AsyncEngine, seeded_spaces: None
) -> None:
    question = "постгрес"
    base = await question_vector(question)
    lexical_id, lexical_capture = await _add_note(
        schema_engine, ACCESS_A, "заметка про постгрес и индексы"
    )
    semantic_id, semantic_capture = await _add_note(
        schema_engine, ACCESS_A, "совсем другая тема без пересечений"
    )
    both_id, both_capture = await _add_note(
        schema_engine, ACCESS_A, "постгрес гибридная запись"
    )
    await _store_chunks(
        engine,
        ACCESS_A,
        record_id=semantic_id,
        capture_event_id=semantic_capture,
        chunks=(make_chunk(0, "семантический чанк", base),),
    )
    await _store_chunks(
        engine,
        ACCESS_A,
        record_id=both_id,
        capture_event_id=both_capture,
        chunks=(make_chunk(0, "чанк гибридной записи", near_vector(base)),),
    )

    model = FakeEmbeddingModel()
    bundle = await retrieve_from_postgres(engine, ACCESS_A, question, model)

    assert model.query_calls == [question]
    assert model.document_calls == []
    assert {
        (chunk.record_id, chunk.chunk_number, chunk.text) for chunk in bundle.chunks
    } == {
        (semantic_id, 0, "семантический чанк"),
        (both_id, 0, "чанк гибридной записи"),
        (lexical_id, None, "заметка про постгрес и индексы"),
    }
    provenance = {
        chunk.record_id: chunk.source_capture_event_id for chunk in bundle.chunks
    }
    assert provenance == {
        semantic_id: semantic_capture,
        both_id: both_capture,
        lexical_id: lexical_capture,
    }
    assert all(chunk.record_kind is SearchRecordType.NOTE for chunk in bundle.chunks)

    again = await retrieve_from_postgres(
        engine, ACCESS_A, question, FakeEmbeddingModel()
    )
    assert again == bundle
    assert repr(again) == repr(bundle)


@pytest.mark.asyncio
async def test_hybrid_never_returns_another_user_space_even_when_closer(
    engine: AsyncEngine, schema_engine: AsyncEngine, seeded_spaces: None
) -> None:
    question = "постгрес"
    base = await question_vector(question)
    own_id, own_capture = await _add_note(
        schema_engine, ACCESS_A, "постгрес заметка своя"
    )
    await _store_chunks(
        engine,
        ACCESS_A,
        record_id=own_id,
        capture_event_id=own_capture,
        chunks=(make_chunk(0, "свой дальний чанк", near_vector(base)),),
    )
    foreign_id, foreign_capture = await _add_note(
        schema_engine, ACCESS_B, "постгрес секрет чужой"
    )
    await _store_chunks(
        engine,
        ACCESS_B,
        record_id=foreign_id,
        capture_event_id=foreign_capture,
        chunks=(make_chunk(0, "чужой точный чанк", base),),
    )

    bundle = await retrieve_from_postgres(
        engine, ACCESS_A, question, FakeEmbeddingModel()
    )

    assert bundle.chunks != ()
    assert {chunk.record_id for chunk in bundle.chunks} == {own_id}
    assert {chunk.source_capture_event_id for chunk in bundle.chunks} == {own_capture}
    assert all("чужой" not in chunk.text for chunk in bundle.chunks)


@pytest.mark.asyncio
async def test_hybrid_hides_completed_alarm_task_on_both_paths(
    engine: AsyncEngine, schema_engine: AsyncEngine, seeded_spaces: None
) -> None:
    # «Задача-будильник»: ЗАВЕРШЕНА и имеет напоминание — не попадает в
    # evidence ни лексическим (заголовок совпадает с вопросом), ни векторным
    # (чанк = вектор вопроса) путём. Та же задача, пока она не завершена, —
    # полноценный кандидат.
    question = "постгрес"
    base = await question_vector(question)
    task_id, task_capture = await _add_task(
        schema_engine,
        ACCESS_A,
        "постгрес задача с будильником",
        status=TaskStatus.COMPLETED,
    )
    await _add_reminder(schema_engine, ACCESS_A, task_id)
    await _store_chunks(
        engine,
        ACCESS_A,
        record_id=task_id,
        capture_event_id=task_capture,
        chunks=(make_chunk(0, "чанк задачи с будильником", base),),
        record_kind=SearchRecordType.TASK,
    )

    hidden = await retrieve_from_postgres(
        engine, ACCESS_A, question, FakeEmbeddingModel()
    )

    assert hidden.chunks == ()

    async with schema_engine.begin() as connection:
        await connection.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(status=TaskStatus.INBOX)
        )

    reopened = await retrieve_from_postgres(
        engine, ACCESS_A, question, FakeEmbeddingModel()
    )

    assert {chunk.record_id for chunk in reopened.chunks} == {task_id}

import hashlib
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.classification_completion import (
    ClassificationCompletionInTransaction,
)
from second_brain.bootstrap.classification_source import (
    PostgresClassificationSourceReader,
)
from second_brain.bootstrap.classification_worker import ClassificationWorker
from second_brain.bootstrap.indexing_completion import (
    CompleteIndexingCommand,
    IndexingCompletionInTransaction,
    StaleSemanticIndexError,
)
from second_brain.bootstrap.indexing_source import (
    IndexingTargetMismatchError,
    PostgresIndexingSourceReader,
    ReadIndexingSourceCommand,
)
from second_brain.bootstrap.indexing_worker import IndexingWorker
from second_brain.bootstrap.local_voice_worker import process_access_once
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.bootstrap.voice_processing_completion import (
    VoiceDownloadCompletionInTransaction,
    VoiceTranscriptionCompletionInTransaction,
)
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.classification.application.contracts import (
    ClassificationDraft,
    ClassificationRequest,
)
from second_brain.slices.classification.application.extraction import ClassifySource
from second_brain.slices.classification.domain.entities import (
    CandidateModality,
    CandidateType,
    ClassificationCandidateDraft,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.knowledge.adapters.persistence.models import (
    IdeaModel,
    NoteModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    CreateTextProcessingRunCommand,
    CreateVoiceProcessingRunCommand,
    SendProcessingNoticeCommand,
    StoredVoice,
    TranscriptionDraft,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingRun,
    ProcessingStep,
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from second_brain.slices.retrieval.adapters.embedding.e5 import EmbeddingFailure
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
    SemanticDocumentModel,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_MODEL_NAME,
    INDEX_VERSION,
    IndexingOutcome,
    RegisterIndexingTargetCommand,
    StoreSemanticChunksCommand,
)
from second_brain.slices.retrieval.application.indexing import IndexSource
from second_brain.slices.retrieval.domain.entities import (
    IndexedChunk,
    SearchRecordType,
)
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.embedding_fakes import FakeEmbeddingModel

NOW = datetime(2026, 7, 14, 17, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
ACCESS = AccessContext(
    UUID("50000000-0000-0000-0000-000000000005"),
    UUID("50000000-0000-0000-0000-000000000015"),
)
ACCESS_B = AccessContext(
    UUID("60000000-0000-0000-0000-000000000006"),
    UUID("60000000-0000-0000-0000-000000000016"),
)
TEXT = "Надо изучить pgvector. Он ускорит семантический поиск."
TRANSCRIPT = "Надо проверить локальную индексацию. Голос уже расшифрован."
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}")


@pytest_asyncio.fixture(autouse=True)
async def reset_indexing_schema(
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
                    "id": access.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS, ACCESS_B)
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": access.user_space_id,
                    "owner_user_id": access.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS, ACCESS_B)
            ],
        )


def _trace(update_id: int) -> str:
    return f"{update_id:x}".rjust(32, "6")[-32:]


async def _capture_text(
    engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
    text: str = TEXT,
) -> UUID:
    port = TaskCaptureInTransaction()
    async with create_session_factory(engine)() as session:
        async with session.begin():
            source = await port.capture(
                CaptureTextCommand(
                    access_context=access,
                    bot_id=1,
                    telegram_update_id=update_id,
                    telegram_message_id=update_id + 1_000,
                    raw_text=text,
                    received_at=NOW,
                    trace_id=_trace(update_id),
                ),
                PostgresUpdateTransaction(session),
            )
    return source.id


async def _add_capture(
    schema_engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
    source_kind: str = "text",
    raw_text: str | None = "direct parent",
) -> UUID:
    capture_event_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_event_id,
                user_space_id=access.user_space_id,
                source_kind=source_kind,
                channel="telegram",
                bot_id=1,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 1_000,
                raw_text=raw_text,
                received_at=NOW,
                created_at=NOW,
                trace_id=_trace(update_id),
            )
        )
    return capture_event_id


async def _create_text_run(
    engine: AsyncEngine,
    access: AccessContext,
    capture_event_id: UUID,
    *,
    update_id: int,
) -> ProcessingRun:
    repository = PostgresProcessingRepository(create_session_factory(engine))
    return await repository.create_text_run(
        CreateTextProcessingRunCommand(
            access_context=access,
            capture_event_id=capture_event_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=_trace(update_id),
        )
    )


async def _register_target(
    engine: AsyncEngine,
    access: AccessContext,
    *,
    processing_run_id: UUID,
    record_kind: SearchRecordType,
    record_id: UUID,
    update_id: int,
) -> None:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresSemanticIndexWriter(session).register_target(
                RegisterIndexingTargetCommand(
                    access_context=access,
                    processing_run_id=processing_run_id,
                    record_kind=record_kind,
                    record_id=record_id,
                    created_at=NOW,
                    trace_id=_trace(update_id),
                )
            )


def _build_worker(
    engine: AsyncEngine,
    embedding_model: FakeEmbeddingModel,
    *,
    completion: object | None = None,
) -> tuple[PostgresProcessingRepository, IndexingWorker]:
    session_factory = create_session_factory(engine)
    repository = PostgresProcessingRepository(session_factory)
    worker = IndexingWorker(
        queue=repository,
        source_reader=PostgresIndexingSourceReader(session_factory),
        indexer=IndexSource(embedding_model),
        completion=completion or IndexingCompletionInTransaction(session_factory),
    )
    return repository, worker


async def _semantic_rows(schema_engine: AsyncEngine) -> list[SemanticDocumentModel]:
    async with create_session_factory(schema_engine)() as session:
        rows = (await session.execute(select(SemanticDocumentModel))).scalars().all()
        return list(rows)


async def _target_rows(schema_engine: AsyncEngine) -> list[IndexingTargetModel]:
    async with create_session_factory(schema_engine)() as session:
        rows = (await session.execute(select(IndexingTargetModel))).scalars().all()
        return list(rows)


def _step(run: ProcessingRun, step_type: ProcessingStepType) -> ProcessingStep:
    return next(step for step in run.steps if step.step_type is step_type)


async def _load_run(
    repository: PostgresProcessingRepository, access: AccessContext, run_id: UUID
) -> ProcessingRun:
    run = await repository.get_run(access, run_id)
    assert run is not None
    return run


async def _single_run_id(schema_engine: AsyncEngine) -> UUID:
    targets = await _target_rows(schema_engine)
    assert len(targets) == 1
    return targets[0].processing_run_id


class RecordingClassificationModel:
    async def classify(self, request: ClassificationRequest) -> ClassificationDraft:
        return ClassificationDraft(
            model_name="recording-local-model",
            prompt_version="test-prompt-v1",
            schema_version="test-schema-v1",
            candidates=(
                ClassificationCandidateDraft(
                    candidate_type=CandidateType.NOTE,
                    source_quote="Голос уже расшифрован.",
                    modality=CandidateModality.OBSERVATION,
                    confidence=0.95,
                ),
            ),
            discarded_candidate_count=0,
        )


class FlakyCompletion:
    def __init__(self, inner: IndexingCompletionInTransaction) -> None:
        self._inner = inner
        self.calls = 0

    async def complete(self, command: CompleteIndexingCommand) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("completion unavailable")
        await self._inner.complete(command)


class IdleStepWorker:
    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        return False


class UnusedIdentity:
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        raise AssertionError("identity must not be used without a notice")

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        raise AssertionError("identity must not be used without a notice")


class UnusedNotifier:
    async def send(self, command: SendProcessingNoticeCommand) -> None:
        raise AssertionError("notifier must not be used without a notice")


async def _seed_voice_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    *,
    update_id: int,
) -> tuple[PostgresProcessingRepository, UUID, UUID]:
    capture_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_id,
                user_space_id=ACCESS.user_space_id,
                source_kind="voice",
                channel="telegram",
                bot_id=1,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 1_000,
                raw_text=None,
                received_at=NOW,
                created_at=NOW,
                trace_id=_trace(update_id),
            )
        )
        await connection.execute(
            insert(TelegramAttachmentModel).values(
                id=uuid4(),
                user_space_id=ACCESS.user_space_id,
                capture_event_id=capture_id,
                kind="voice",
                telegram_file_id="private-file-id",
                telegram_file_unique_id="private-unique-id",
                duration_seconds=2,
                telegram_file_size=10,
                telegram_mime_type="audio/ogg",
                storage_key=None,
                sha256=None,
                stored_size=None,
                stored_mime_type=None,
                stored_at=None,
                created_at=NOW,
                trace_id=_trace(update_id),
            )
        )
    session_factory = create_session_factory(engine)
    repository = PostgresProcessingRepository(session_factory)
    run = await repository.create_voice_run(
        CreateVoiceProcessingRunCommand(
            access_context=ACCESS,
            capture_event_id=capture_id,
            output_type=TranscriptionOutputType.IDEA,
            created_at=NOW,
            trace_id=_trace(update_id),
        )
    )
    return repository, capture_id, run.id


async def _transcribe_voice_run(
    engine: AsyncEngine,
    repository: PostgresProcessingRepository,
    capture_id: UUID,
    *,
    completed_at: datetime,
) -> None:
    session_factory = create_session_factory(engine)
    voice_types = (
        ProcessingStepType.AUDIO_DOWNLOAD,
        ProcessingStepType.TRANSCRIPTION,
    )
    download = await repository.claim_due_step(ACCESS, NOW, LEASE, voice_types)
    assert download is not None
    await VoiceDownloadCompletionInTransaction(session_factory).complete(
        CompleteVoiceDownloadCommand(
            access_context=ACCESS,
            step_id=download.step_id,
            capture_event_id=capture_id,
            stored_voice=StoredVoice(
                storage_key="private/original.ogg",
                local_path="/private/original.ogg",
                sha256="d" * 64,
                size=10,
                mime_type="audio/ogg",
            ),
            completed_at=NOW + timedelta(seconds=1),
        )
    )
    transcription = await repository.claim_due_step(
        ACCESS, NOW + timedelta(seconds=1), LEASE, voice_types
    )
    assert transcription is not None
    await VoiceTranscriptionCompletionInTransaction(session_factory).complete(
        CompleteVoiceTranscriptionCommand(
            access_context=ACCESS,
            step_id=transcription.step_id,
            draft=TranscriptionDraft(
                text=TRANSCRIPT,
                language="ru",
                language_probability=0.99,
                model_name="local-whisper",
                segments=(),
            ),
            completed_at=completed_at,
        )
    )


async def _complete_voice_transcription(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    *,
    update_id: int,
    completed_at: datetime,
) -> tuple[PostgresProcessingRepository, UUID, UUID]:
    repository, capture_id, run_id = await _seed_voice_run(
        engine, schema_engine, update_id=update_id
    )
    await _transcribe_voice_run(
        engine, repository, capture_id, completed_at=completed_at
    )
    return repository, capture_id, run_id


@pytest.mark.asyncio
async def test_text_capture_registers_target_and_worker_indexes_note(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_id = await _capture_text(engine, ACCESS, update_id=501)

    targets = await _target_rows(schema_engine)
    assert len(targets) == 1
    async with create_session_factory(schema_engine)() as session:
        note = (await session.execute(select(NoteModel))).scalars().one()
    assert targets[0].record_kind is SearchRecordType.NOTE
    assert targets[0].record_id == note.id
    assert targets[0].user_space_id == ACCESS.user_space_id

    embedding_model = FakeEmbeddingModel()
    repository, worker = _build_worker(engine, embedding_model)
    worked = await worker.process_once(ACCESS, NOW + timedelta(seconds=5))
    assert worked is True

    run = await _load_run(repository, ACCESS, targets[0].processing_run_id)
    assert (
        _step(run, ProcessingStepType.INDEXING).status is ProcessingStepStatus.SUCCEEDED
    )
    assert (
        _step(run, ProcessingStepType.CLASSIFICATION).status
        is ProcessingStepStatus.PENDING
    )

    expected_chunks = await FakeEmbeddingModel().embed_document(TEXT)
    rows = await _semantic_rows(schema_engine)
    assert len(rows) == len(expected_chunks) >= 2
    assert sorted(row.chunk_number for row in rows) == list(range(len(expected_chunks)))
    for row in rows:
        assert row.source_kind is SearchRecordType.NOTE
        assert row.source_record_id == note.id
        assert row.source_capture_event_id == capture_id
        assert row.user_space_id == ACCESS.user_space_id
        assert row.embedding_model == EMBEDDING_MODEL_NAME
        assert row.index_version == INDEX_VERSION
        assert row.trace_id == run.trace_id
    assert {row.content_sha256 for row in rows} == {
        chunk.content_sha256 for chunk in expected_chunks
    }


@pytest.mark.asyncio
async def test_semantic_chunks_carry_record_created_at_not_completion_time(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _capture_text(engine, ACCESS, update_id=505)
    async with create_session_factory(schema_engine)() as session:
        note = (await session.execute(select(NoteModel))).scalars().one()
    completed_at = NOW + timedelta(seconds=5)
    assert note.created_at != completed_at

    _, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, completed_at) is True

    rows = await _semantic_rows(schema_engine)
    assert rows != []
    assert {row.created_at for row in rows} == {note.created_at}


@pytest.mark.asyncio
async def test_voice_indexing_waits_for_transcription_then_indexes_transcript(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, capture_id, run_id = await _seed_voice_run(
        engine, schema_engine, update_id=511
    )
    _, worker = _build_worker(engine, FakeEmbeddingModel())

    assert await worker.process_once(ACCESS, NOW) is False
    assert await _target_rows(schema_engine) == []

    await _transcribe_voice_run(
        engine, repository, capture_id, completed_at=NOW + timedelta(seconds=2)
    )
    voice_targets = [
        target
        for target in await _target_rows(schema_engine)
        if target.processing_run_id == run_id
    ]
    assert len(voice_targets) == 1
    async with create_session_factory(schema_engine)() as session:
        idea = (await session.execute(select(IdeaModel))).scalars().one()
    assert voice_targets[0].record_kind is SearchRecordType.IDEA
    assert voice_targets[0].record_id == idea.id

    _, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW + timedelta(seconds=3)) is True

    expected_chunks = await FakeEmbeddingModel().embed_document(TRANSCRIPT)
    rows = [
        row
        for row in await _semantic_rows(schema_engine)
        if row.source_record_id == idea.id
    ]
    assert len(rows) == len(expected_chunks)
    assert {row.chunk_text for row in rows} == {chunk.text for chunk in expected_chunks}
    assert all(row.source_capture_event_id == capture_id for row in rows)


@pytest.mark.asyncio
async def test_equal_created_at_sibling_with_smaller_uuid_is_not_indexed(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    completed_at = NOW + timedelta(seconds=2)
    repository, capture_id, run_id = await _complete_voice_transcription(
        engine, schema_engine, update_id=521, completed_at=completed_at
    )
    async with create_session_factory(schema_engine)() as session:
        target_idea = (await session.execute(select(IdeaModel))).scalars().one()
    sibling_id = UUID("00000000-0000-0000-0000-0000000000aa")
    assert str(sibling_id) < str(target_idea.id)
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(IdeaModel).values(
                id=sibling_id,
                user_space_id=ACCESS.user_space_id,
                text="SIBLING BAIT",
                source_capture_event_id=capture_id,
                created_at=completed_at,
                updated_at=completed_at,
                trace_id=_trace(521),
            )
        )

    _, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW + timedelta(seconds=4)) is True

    rows = await _semantic_rows(schema_engine)
    assert rows != []
    assert {row.source_record_id for row in rows} == {target_idea.id}
    assert all("SIBLING BAIT" not in row.chunk_text for row in rows)
    run = await _load_run(repository, ACCESS, run_id)
    assert (
        _step(run, ProcessingStepType.INDEXING).status is ProcessingStepStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_classifier_sibling_materialized_first_is_not_indexed(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _, _ = await _complete_voice_transcription(
        engine, schema_engine, update_id=531, completed_at=NOW + timedelta(seconds=2)
    )
    session_factory = create_session_factory(engine)
    classification_worker = ClassificationWorker(
        queue=repository,
        source_reader=PostgresClassificationSourceReader(session_factory),
        classifier=ClassifySource(RecordingClassificationModel()),
        completion=ClassificationCompletionInTransaction(session_factory),
    )
    assert (
        await classification_worker.process_once(ACCESS, NOW + timedelta(seconds=3))
        is True
    )
    async with create_session_factory(schema_engine)() as session:
        sibling_note = (await session.execute(select(NoteModel))).scalars().one()
        target_idea = (await session.execute(select(IdeaModel))).scalars().one()

    _, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW + timedelta(seconds=4)) is True

    rows = await _semantic_rows(schema_engine)
    assert rows != []
    assert {row.source_record_id for row in rows} == {target_idea.id}
    assert all(row.source_record_id != sibling_note.id for row in rows)


@pytest.mark.asyncio
async def test_target_with_unknown_or_foreign_record_indexes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_a = await _add_capture(schema_engine, ACCESS, update_id=541)
    run_unknown = await _create_text_run(engine, ACCESS, capture_a, update_id=541)
    await _register_target(
        engine,
        ACCESS,
        processing_run_id=run_unknown.id,
        record_kind=SearchRecordType.NOTE,
        record_id=uuid4(),
        update_id=541,
    )

    capture_b = await _add_capture(schema_engine, ACCESS_B, update_id=542)
    foreign_note_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(NoteModel).values(
                id=foreign_note_id,
                user_space_id=ACCESS_B.user_space_id,
                text="FOREIGN SECRET",
                source_capture_event_id=capture_b,
                created_at=NOW,
                updated_at=NOW,
                trace_id=_trace(542),
            )
        )
    capture_a2 = await _add_capture(schema_engine, ACCESS, update_id=543)
    run_foreign = await _create_text_run(engine, ACCESS, capture_a2, update_id=543)
    await _register_target(
        engine,
        ACCESS,
        processing_run_id=run_foreign.id,
        record_kind=SearchRecordType.NOTE,
        record_id=foreign_note_id,
        update_id=543,
    )

    repository, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW) is True
    assert await worker.process_once(ACCESS, NOW) is True

    assert await _semantic_rows(schema_engine) == []
    for run_id in (run_unknown.id, run_foreign.id):
        run = await _load_run(repository, ACCESS, run_id)
        step = _step(run, ProcessingStepType.INDEXING)
        assert step.status is ProcessingStepStatus.PENDING
        assert step.safe_error_code == "indexing_target_mismatch"


@pytest.mark.asyncio
async def test_target_of_another_capture_fails_mismatch_without_leak(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    other_capture = await _add_capture(schema_engine, ACCESS, update_id=551)
    other_note_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(NoteModel).values(
                id=other_note_id,
                user_space_id=ACCESS.user_space_id,
                text="OTHER CAPTURE SECRET",
                source_capture_event_id=other_capture,
                created_at=NOW,
                updated_at=NOW,
                trace_id=_trace(551),
            )
        )
    run_capture = await _add_capture(schema_engine, ACCESS, update_id=552)
    run = await _create_text_run(engine, ACCESS, run_capture, update_id=552)
    await _register_target(
        engine,
        ACCESS,
        processing_run_id=run.id,
        record_kind=SearchRecordType.NOTE,
        record_id=other_note_id,
        update_id=552,
    )

    reader = PostgresIndexingSourceReader(create_session_factory(engine))
    with pytest.raises(IndexingTargetMismatchError) as excinfo:
        await reader.read(
            ReadIndexingSourceCommand(access_context=ACCESS, processing_run_id=run.id)
        )
    message = str(excinfo.value)
    assert message == "indexing_target_mismatch"
    assert "OTHER CAPTURE SECRET" not in message
    assert UUID_PATTERN.search(message) is None

    repository, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW) is True

    assert await _semantic_rows(schema_engine) == []
    loaded = await _load_run(repository, ACCESS, run.id)
    step = _step(loaded, ProcessingStepType.INDEXING)
    assert step.status is ProcessingStepStatus.PENDING
    assert step.safe_error_code == "indexing_target_mismatch"


@pytest.mark.asyncio
async def test_embedding_failure_retries_then_fails_without_notice(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _capture_text(engine, ACCESS, update_id=561)
    run_id = await _single_run_id(schema_engine)
    failing_model = FakeEmbeddingModel(error=EmbeddingFailure("embedding_failed"))
    repository, worker = _build_worker(engine, failing_model)

    at = NOW + timedelta(seconds=5)
    for attempt in range(1, 4):
        assert await worker.process_once(ACCESS, at) is True
        run = await _load_run(repository, ACCESS, run_id)
        step = _step(run, ProcessingStepType.INDEXING)
        assert step.attempt_count == attempt
        assert step.safe_error_code == "embedding_failed"
        if attempt < 3:
            assert step.status is ProcessingStepStatus.PENDING
            assert step.next_attempt_at is not None
            at = step.next_attempt_at

    run = await _load_run(repository, ACCESS, run_id)
    final = _step(run, ProcessingStepType.INDEXING)
    assert final.status is ProcessingStepStatus.FAILED
    assert final.next_attempt_at is None
    assert await _semantic_rows(schema_engine) == []
    assert await repository.claim_due_notice(ACCESS, at + timedelta(days=1)) is None


@pytest.mark.asyncio
async def test_completion_crash_then_retry_writes_exactly_one_chunk_set(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _capture_text(engine, ACCESS, update_id=571)
    run_id = await _single_run_id(schema_engine)
    session_factory = create_session_factory(engine)
    flaky = FlakyCompletion(IndexingCompletionInTransaction(session_factory))
    repository, worker = _build_worker(engine, FakeEmbeddingModel(), completion=flaky)

    assert await worker.process_once(ACCESS, NOW + timedelta(seconds=5)) is True
    run = await _load_run(repository, ACCESS, run_id)
    step = _step(run, ProcessingStepType.INDEXING)
    assert step.status is ProcessingStepStatus.PENDING
    assert step.safe_error_code == "indexing_failed"
    assert await _semantic_rows(schema_engine) == []

    assert step.next_attempt_at is not None
    assert await worker.process_once(ACCESS, step.next_attempt_at) is True
    run = await _load_run(repository, ACCESS, run_id)
    assert (
        _step(run, ProcessingStepType.INDEXING).status is ProcessingStepStatus.SUCCEEDED
    )
    expected_chunks = await FakeEmbeddingModel().embed_document(TEXT)
    rows = await _semantic_rows(schema_engine)
    assert len(rows) == len(expected_chunks)
    assert sorted(row.chunk_number for row in rows) == list(range(len(expected_chunks)))


@pytest.mark.asyncio
async def test_existing_matching_chunks_succeed_without_new_rows(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_id = await _capture_text(engine, ACCESS, update_id=581)
    run_id = await _single_run_id(schema_engine)
    async with create_session_factory(schema_engine)() as session:
        note = (await session.execute(select(NoteModel))).scalars().one()
    chunks = await FakeEmbeddingModel().embed_document(TEXT)
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresSemanticIndexWriter(session).insert_chunks(
                StoreSemanticChunksCommand(
                    access_context=ACCESS,
                    record_kind=SearchRecordType.NOTE,
                    record_id=note.id,
                    source_capture_event_id=capture_id,
                    chunks=chunks,
                    embedding_model=EMBEDDING_MODEL_NAME,
                    index_version=INDEX_VERSION,
                    created_at=NOW,
                    trace_id=_trace(581),
                )
            )
    before = {row.id for row in await _semantic_rows(schema_engine)}
    assert len(before) == len(chunks)

    repository, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW + timedelta(seconds=5)) is True

    after = {row.id for row in await _semantic_rows(schema_engine)}
    assert after == before
    run = await _load_run(repository, ACCESS, run_id)
    assert (
        _step(run, ProcessingStepType.INDEXING).status is ProcessingStepStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_diverged_preseeded_chunk_fails_stale_without_mixing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_id = await _capture_text(engine, ACCESS, update_id=591)
    run_id = await _single_run_id(schema_engine)
    async with create_session_factory(schema_engine)() as session:
        note = (await session.execute(select(NoteModel))).scalars().one()
    divergent = IndexedChunk(
        chunk_number=0,
        content_sha256=hashlib.sha256(b"a different text version").hexdigest(),
        text="seeded divergent chunk",
        embedding=tuple([1.0] + [0.0] * 767),
    )
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await PostgresSemanticIndexWriter(session).insert_chunks(
                StoreSemanticChunksCommand(
                    access_context=ACCESS,
                    record_kind=SearchRecordType.NOTE,
                    record_id=note.id,
                    source_capture_event_id=capture_id,
                    chunks=(divergent,),
                    embedding_model=EMBEDDING_MODEL_NAME,
                    index_version=INDEX_VERSION,
                    created_at=NOW,
                    trace_id=_trace(591),
                )
            )

    repository, worker = _build_worker(engine, FakeEmbeddingModel())
    assert await worker.process_once(ACCESS, NOW + timedelta(seconds=5)) is True

    run = await _load_run(repository, ACCESS, run_id)
    step = _step(run, ProcessingStepType.INDEXING)
    assert step.status is ProcessingStepStatus.PENDING
    assert step.safe_error_code == "stale_semantic_index"
    rows = await _semantic_rows(schema_engine)
    assert len(rows) == 1
    assert rows[0].content_sha256 == divergent.content_sha256

    assert step.next_attempt_at is not None
    claim = await repository.claim_due_step(
        ACCESS, step.next_attempt_at, LEASE, (ProcessingStepType.INDEXING,)
    )
    assert claim is not None
    session_factory = create_session_factory(engine)
    source = await PostgresIndexingSourceReader(session_factory).read(
        ReadIndexingSourceCommand(access_context=ACCESS, processing_run_id=run_id)
    )
    outcome = await IndexSource(FakeEmbeddingModel()).execute(source)
    completion = IndexingCompletionInTransaction(session_factory)
    with pytest.raises(StaleSemanticIndexError) as excinfo:
        await completion.complete(
            CompleteIndexingCommand(
                access_context=ACCESS,
                step_id=claim.step_id,
                outcome=outcome,
                completed_at=step.next_attempt_at,
            )
        )
    message = str(excinfo.value)
    assert message == "stale_semantic_index"
    assert "seeded divergent chunk" not in message
    assert TEXT not in message
    assert len(await _semantic_rows(schema_engine)) == 1


@pytest.mark.asyncio
async def test_process_access_once_handles_one_indexing_step_per_space(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _capture_text(engine, ACCESS, update_id=601)
    await _capture_text(engine, ACCESS, update_id=602, text="Вторая заметка.")
    repository, worker = _build_worker(engine, FakeEmbeddingModel())

    async def _succeeded_indexing_count() -> int:
        count = 0
        for target in await _target_rows(schema_engine):
            run = await _load_run(repository, ACCESS, target.processing_run_id)
            step = _step(run, ProcessingStepType.INDEXING)
            if step.status is ProcessingStepStatus.SUCCEEDED:
                count += 1
        return count

    worked = await process_access_once(
        access_context=ACCESS,
        now=NOW + timedelta(seconds=5),
        worker=IdleStepWorker(),
        classification_worker=IdleStepWorker(),
        indexing_worker=worker,
        processing_repository=repository,
        identity_repository=UnusedIdentity(),
        notifier=UnusedNotifier(),
    )
    assert worked is True
    assert await _succeeded_indexing_count() == 1

    worked = await process_access_once(
        access_context=ACCESS,
        now=NOW + timedelta(seconds=6),
        worker=IdleStepWorker(),
        classification_worker=IdleStepWorker(),
        indexing_worker=worker,
        processing_repository=repository,
        identity_repository=UnusedIdentity(),
        notifier=UnusedNotifier(),
    )
    assert worked is True
    assert await _succeeded_indexing_count() == 2


def test_indexing_commands_and_errors_leak_no_content_or_ids() -> None:
    read_command = ReadIndexingSourceCommand(
        access_context=ACCESS, processing_run_id=uuid4()
    )
    complete_command = CompleteIndexingCommand(
        access_context=ACCESS,
        step_id=uuid4(),
        outcome=IndexingOutcome(
            record_kind=SearchRecordType.NOTE,
            record_id=uuid4(),
            chunks=(
                IndexedChunk(
                    chunk_number=0,
                    content_sha256="a" * 64,
                    text="very private text",
                    embedding=(0.5,),
                ),
            ),
            created_at=NOW,
        ),
        completed_at=NOW,
    )
    for value in (repr(read_command), repr(complete_command)):
        assert "very private text" not in value
        assert UUID_PATTERN.search(value) is None
    assert str(IndexingTargetMismatchError()) == "indexing_target_mismatch"
    assert str(StaleSemanticIndexError()) == "stale_semantic_index"

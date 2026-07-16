from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.classification_completion import (
    ClassificationCompletionInTransaction,
)
from second_brain.bootstrap.classification_source import (
    PostgresClassificationSourceReader,
)
from second_brain.bootstrap.classification_worker import ClassificationWorker
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.voice_processing_completion import (
    VoiceDownloadCompletionInTransaction,
    VoiceTranscriptionCompletionInTransaction,
)
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.classification.adapters.persistence.models import (
    ClassificationResultModel,
)
from second_brain.slices.classification.application.contracts import (
    ClassificationDraft,
    ClassificationRequest,
    ReadClassificationSourceCommand,
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
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.knowledge.adapters.persistence.models import IdeaModel
from second_brain.slices.processing.adapters.persistence.models import TranscriptModel
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    CreateTextProcessingRunCommand,
    CreateVoiceProcessingRunCommand,
    StoredVoice,
    TranscriptionDraft,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepType,
    TranscriptionOutputType,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("30000000-0000-0000-0000-000000000003"),
    UUID("30000000-0000-0000-0000-000000000013"),
)
ACCESS_B = AccessContext(
    UUID("40000000-0000-0000-0000-000000000004"),
    UUID("40000000-0000-0000-0000-000000000014"),
)
TRACE_ID = "c" * 32
TRANSCRIPT = "Надо проверить Graphiti. Это может помочь проекту."


class NullConfirmationDelivery:
    async def deliver(self, text: str, recipient: TelegramRecipient) -> None:
        return None


class FixedWorkerIdentity:
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        return (ACCESS,)

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        return TelegramRecipient(telegram_user_id=42)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        return Locale.RU


@pytest_asyncio.fixture(autouse=True)
async def reset_worker_schema(
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


class RecordingModel:
    def __init__(self) -> None:
        self.requests: list[ClassificationRequest] = []

    async def classify(self, request: ClassificationRequest) -> ClassificationDraft:
        self.requests.append(request)
        return ClassificationDraft(
            model_name="recording-local-model",
            prompt_version="test-prompt-v1",
            schema_version="test-schema-v1",
            candidates=(
                ClassificationCandidateDraft(
                    candidate_type=CandidateType.TASK,
                    source_quote="Надо проверить Graphiti",
                    modality=CandidateModality.COMMITMENT,
                    confidence=0.96,
                ),
            ),
            discarded_candidate_count=0,
        )


async def _complete_voice_transcription(
    engine: AsyncEngine, schema_engine: AsyncEngine
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
                telegram_update_id=901,
                telegram_message_id=902,
                raw_text=None,
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
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
                trace_id=TRACE_ID,
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
            trace_id=TRACE_ID,
        )
    )
    voice_types = (
        ProcessingStepType.AUDIO_DOWNLOAD,
        ProcessingStepType.TRANSCRIPTION,
    )
    download = await repository.claim_due_step(
        ACCESS, NOW, timedelta(minutes=15), voice_types
    )
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
        ACCESS, NOW + timedelta(seconds=1), timedelta(minutes=15), voice_types
    )
    assert transcription is not None
    await VoiceTranscriptionCompletionInTransaction(
        session_factory, NullConfirmationDelivery(), FixedWorkerIdentity()
    ).complete(
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
            completed_at=NOW + timedelta(seconds=2),
        )
    )
    return repository, capture_id, run.id


@pytest.mark.asyncio
async def test_voice_classification_reads_exact_committed_transcript(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _, _ = await _complete_voice_transcription(engine, schema_engine)
    model = RecordingModel()
    session_factory = create_session_factory(engine)
    worker = ClassificationWorker(
        queue=repository,
        source_reader=PostgresClassificationSourceReader(session_factory),
        classifier=ClassifySource(model),
        completion=ClassificationCompletionInTransaction(
            session_factory, NullConfirmationDelivery(), FixedWorkerIdentity()
        ),
    )

    worked = await worker.process_once(ACCESS, NOW + timedelta(seconds=3))

    assert worked is True
    assert len(model.requests) == 1
    assert model.requests[0].source_text == TRANSCRIPT
    assert TRANSCRIPT not in repr(model.requests[0])
    async with schema_engine.connect() as connection:
        stored_transcript = await connection.scalar(select(TranscriptModel.text))
        result_count = await connection.scalar(
            select(func.count()).select_from(ClassificationResultModel)
        )
        idea_count = await connection.scalar(
            select(func.count()).select_from(IdeaModel)
        )
    assert stored_transcript == TRANSCRIPT
    assert result_count == 1
    assert idea_count == 1


@pytest.mark.asyncio
async def test_worker_and_source_reader_never_cross_or_mismatch_scope(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, capture_id, run_id = await _complete_voice_transcription(
        engine, schema_engine
    )
    model = RecordingModel()
    session_factory = create_session_factory(engine)
    reader = PostgresClassificationSourceReader(session_factory)
    worker = ClassificationWorker(
        queue=repository,
        source_reader=reader,
        classifier=ClassifySource(model),
        completion=ClassificationCompletionInTransaction(
            session_factory, NullConfirmationDelivery(), FixedWorkerIdentity()
        ),
    )

    worked = await worker.process_once(ACCESS_B, NOW + timedelta(seconds=3))

    assert worked is False
    assert model.requests == []
    with pytest.raises(LookupError):
        await reader.read(
            ReadClassificationSourceCommand(
                access_context=ACCESS_B,
                processing_run_id=run_id,
                capture_event_id=capture_id,
                base_type=CandidateType.IDEA,
            )
        )

    text_capture_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=text_capture_id,
                user_space_id=ACCESS.user_space_id,
                source_kind="text",
                channel="telegram",
                bot_id=1,
                telegram_update_id=903,
                telegram_message_id=904,
                raw_text="private text source",
                received_at=NOW,
                created_at=NOW,
                trace_id="e" * 32,
            )
        )
    await repository.create_text_run(
        CreateTextProcessingRunCommand(
            access_context=ACCESS,
            capture_event_id=text_capture_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id="e" * 32,
        )
    )
    with pytest.raises(LookupError):
        await reader.read(
            ReadClassificationSourceCommand(
                access_context=ACCESS,
                processing_run_id=uuid4(),
                capture_event_id=text_capture_id,
                base_type=CandidateType.NOTE,
            )
        )

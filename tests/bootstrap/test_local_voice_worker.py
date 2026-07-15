from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.local_voice_worker import (
    build_classification_model,
    process_access_once,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.voice_processing_completion import (
    VoiceDownloadCompletionInTransaction,
    VoiceTranscriptionCompletionInTransaction,
)
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.classification.adapters.openrouter.model import (
    OpenRouterClassificationModel,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresWorkerIdentityRepository,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.knowledge.adapters.persistence.models import IdeaModel
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingNoticeModel,
    ProcessingStepModel,
    TranscriptModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    SendProcessingNoticeCommand,
    StoredVoice,
    TranscriptionDraft,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingNoticeClaim,
    ProcessingNoticeKind,
    ProcessingNoticeStatus,
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
    TranscriptSegment,
    TranscriptWord,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)
TRACE_ID = "7" * 32


def _settings(open_router_ai_key: str | None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://app@example/database",
        schema_database_url="postgresql+asyncpg://owner@example/database",
        telegram_bot_token="bot-token",
        invite_token_pepper=b"pepper",
        invite_token_pepper_key_id="local-v1",
        open_router_ai_key=open_router_ai_key,
    )


def test_worker_requires_openrouter_key_before_composition() -> None:
    with pytest.raises(RuntimeError, match="OPEN_ROUTER_AI_KEY must be configured"):
        build_classification_model(_settings(None))


def test_worker_builds_openrouter_classifier_from_configured_key() -> None:
    model = build_classification_model(_settings("private-openrouter-key"))

    assert isinstance(model, OpenRouterClassificationModel)
    assert "private-openrouter-key" not in repr(model)


@pytest_asyncio.fixture(autouse=True)
async def voice_worker_database(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="member",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=ACCESS.user_space_id,
                owner_user_id=ACCESS.user_id,
                timezone="Asia/Jerusalem",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(TelegramIdentity).values(
                id=uuid4(),
                telegram_user_id=555,
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


async def _create_claimed_transcription(
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
                bot_id=10,
                telegram_update_id=20,
                telegram_message_id=30,
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
                telegram_file_size=12,
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
    voice_steps = (
        ProcessingStepType.AUDIO_DOWNLOAD,
        ProcessingStepType.TRANSCRIPTION,
    )
    download = await repository.claim_due_step(
        ACCESS, NOW, timedelta(minutes=15), voice_steps
    )
    assert download is not None
    await VoiceDownloadCompletionInTransaction(session_factory).complete(
        CompleteVoiceDownloadCommand(
            access_context=ACCESS,
            step_id=download.step_id,
            capture_event_id=capture_id,
            stored_voice=StoredVoice(
                storage_key=f"{ACCESS.user_space_id}/{capture_id}/original.ogg",
                local_path="/private/audio.ogg",
                sha256="a" * 64,
                size=12,
                mime_type="audio/ogg",
            ),
            completed_at=NOW + timedelta(seconds=1),
        )
    )
    transcription = await repository.claim_due_step(
        ACCESS,
        NOW + timedelta(seconds=1),
        timedelta(minutes=15),
        voice_steps,
    )
    assert transcription is not None
    assert transcription.step_type is ProcessingStepType.TRANSCRIPTION
    return repository, run.id, transcription.step_id


def _draft(*, language: str = "ru") -> TranscriptionDraft:
    return TranscriptionDraft(
        text="голосовая идея",
        language=language,
        language_probability=0.98,
        model_name="whisper-test-model",
        segments=(
            TranscriptSegment(
                start=0.0,
                end=1.0,
                text="голосовая идея",
                words=(TranscriptWord(0.0, 1.0, "голосовая идея"),),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_completion_atomically_creates_transcript_frozen_type_and_notice(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, run_id, step_id = await _create_claimed_transcription(engine, schema_engine)

    await VoiceTranscriptionCompletionInTransaction(
        create_session_factory(engine)
    ).complete(
        CompleteVoiceTranscriptionCommand(
            access_context=ACCESS,
            step_id=step_id,
            draft=_draft(),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    async with schema_engine.connect() as connection:
        transcript = (
            await connection.execute(
                select(
                    TranscriptModel.capture_event_id,
                    TranscriptModel.text,
                    TranscriptModel.segments,
                )
            )
        ).one()
        idea = (
            await connection.execute(
                select(IdeaModel.source_capture_event_id, IdeaModel.text)
            )
        ).one()
        notice = (
            await connection.execute(
                select(
                    ProcessingNoticeModel.processing_run_id,
                    ProcessingNoticeModel.kind,
                    ProcessingNoticeModel.status,
                )
            )
        ).one()
        step_status = await connection.scalar(
            select(ProcessingStepModel.status).where(ProcessingStepModel.id == step_id)
        )
    assert transcript.capture_event_id == idea.source_capture_event_id
    assert transcript.text == idea.text == "голосовая идея"
    assert transcript.segments[0]["words"][0]["text"] == "голосовая идея"
    assert notice.processing_run_id == run_id
    assert notice.kind is ProcessingNoticeKind.SUCCESS
    assert notice.status is ProcessingNoticeStatus.PENDING
    assert step_status == ProcessingStepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_failed_completion_rolls_back_then_retry_creates_one_result(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _, step_id = await _create_claimed_transcription(engine, schema_engine)
    completion = VoiceTranscriptionCompletionInTransaction(
        create_session_factory(engine)
    )

    with pytest.raises(DBAPIError):
        await completion.complete(
            CompleteVoiceTranscriptionCommand(
                access_context=ACCESS,
                step_id=step_id,
                draft=_draft(language="language-is-longer-than-column"),
                completed_at=NOW + timedelta(seconds=2),
            )
        )

    async with schema_engine.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(TranscriptModel))
            == 0
        )
        assert await connection.scalar(select(func.count()).select_from(IdeaModel)) == 0
        assert (
            await connection.scalar(
                select(func.count()).select_from(ProcessingNoticeModel)
            )
            == 0
        )

    failed_at = NOW + timedelta(seconds=3)
    retry = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS,
            step_id=step_id,
            failed_at=failed_at,
            safe_error_code="completion_failed",
        )
    )
    claim = await repository.claim_due_step(
        ACCESS,
        retry.next_attempt_at or failed_at,
        timedelta(minutes=15),
        (ProcessingStepType.TRANSCRIPTION,),
    )
    assert claim is not None
    await completion.complete(
        CompleteVoiceTranscriptionCommand(
            access_context=ACCESS,
            step_id=claim.step_id,
            draft=_draft(),
            completed_at=failed_at + timedelta(minutes=1),
        )
    )

    async with schema_engine.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(TranscriptModel))
            == 1
        )
        assert await connection.scalar(select(func.count()).select_from(IdeaModel)) == 1
        assert (
            await connection.scalar(
                select(func.count()).select_from(ProcessingNoticeModel)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_other_space_cannot_complete_or_observe_the_transcription(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, _, step_id = await _create_claimed_transcription(engine, schema_engine)
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS_B.user_id,
                role="member",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=ACCESS_B.user_space_id,
                owner_user_id=ACCESS_B.user_id,
                timezone="Asia/Jerusalem",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with pytest.raises(LookupError):
        await VoiceTranscriptionCompletionInTransaction(
            create_session_factory(engine)
        ).complete(
            CompleteVoiceTranscriptionCommand(
                access_context=ACCESS_B,
                step_id=step_id,
                draft=_draft(),
                completed_at=NOW + timedelta(seconds=2),
            )
        )

    async with schema_engine.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(TranscriptModel))
            == 0
        )
        assert await connection.scalar(select(func.count()).select_from(IdeaModel)) == 0


@pytest.mark.asyncio
async def test_worker_derives_active_scope_and_recipient_from_identity_mapping(
    engine: AsyncEngine,
) -> None:
    repository = PostgresWorkerIdentityRepository(create_session_factory(engine))

    assert await repository.list_active_access_contexts() == (ACCESS,)
    recipient = await repository.resolve_telegram_recipient(ACCESS)

    assert recipient.telegram_user_id == 555
    assert "555" not in repr(recipient)
    mismatched = AccessContext(ACCESS.user_id, uuid4())
    with pytest.raises(LookupError):
        await repository.resolve_telegram_recipient(mismatched)


class FakeVoiceWorker:
    def __init__(self, worked: bool) -> None:
        self.worked = worked
        self.calls: list[tuple[AccessContext, datetime]] = []

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        self.calls.append((access_context, now))
        return self.worked


class FakeNoticeRepository:
    def __init__(self, claim: ProcessingNoticeClaim | None) -> None:
        self.claim = claim
        self.claim_calls: list[tuple[AccessContext, datetime]] = []
        self.sent: list[object] = []

    async def claim_due_notice(
        self, access_context: AccessContext, now: datetime
    ) -> ProcessingNoticeClaim | None:
        self.claim_calls.append((access_context, now))
        return self.claim

    async def mark_notice_sent(self, command: object) -> None:
        self.sent.append(command)


class FakeWorkerIdentity:
    def __init__(self, locale: Locale = Locale.RU) -> None:
        self.calls: list[AccessContext] = []
        self.locale_calls: list[AccessContext] = []
        self._locale = locale

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        self.calls.append(access_context)
        return TelegramRecipient(telegram_user_id=555)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        self.locale_calls.append(access_context)
        return self._locale


class FakeNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.commands: list[SendProcessingNoticeCommand] = []

    async def send(self, command: SendProcessingNoticeCommand) -> None:
        self.commands.append(command)
        if self.error is not None:
            raise self.error


def _notice_claim() -> ProcessingNoticeClaim:
    return ProcessingNoticeClaim(
        notice_id=uuid4(),
        run_id=uuid4(),
        kind=ProcessingNoticeKind.SUCCESS,
        output_type=TranscriptionOutputType.IDEA,
        trace_id=TRACE_ID,
        attempt_count=1,
    )


@pytest.mark.asyncio
async def test_cycle_processes_one_scope_and_marks_notice_only_after_send() -> None:
    worker = FakeVoiceWorker(worked=False)
    classification_worker = FakeVoiceWorker(worked=False)
    repository = FakeNoticeRepository(_notice_claim())
    identity = FakeWorkerIdentity()
    notifier = FakeNotifier()

    worked = await process_access_once(
        access_context=ACCESS,
        now=NOW,
        worker=worker,
        classification_worker=classification_worker,
        indexing_worker=FakeVoiceWorker(worked=False),
        processing_repository=repository,
        identity_repository=identity,
        notifier=notifier,
    )

    assert worked is True
    assert worker.calls == [(ACCESS, NOW)]
    assert classification_worker.calls == [(ACCESS, NOW)]
    assert repository.claim_calls == [(ACCESS, NOW)]
    assert identity.calls == [ACCESS]
    assert len(notifier.commands) == 1
    assert notifier.commands[0].notice.output_type is TranscriptionOutputType.IDEA
    assert len(repository.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [Locale.RU, Locale.EN])
async def test_cycle_resolves_notice_locale_from_identity(locale: Locale) -> None:
    repository = FakeNoticeRepository(_notice_claim())
    identity = FakeWorkerIdentity(locale=locale)
    notifier = FakeNotifier()

    await process_access_once(
        access_context=ACCESS,
        now=NOW,
        worker=FakeVoiceWorker(worked=False),
        classification_worker=FakeVoiceWorker(worked=False),
        indexing_worker=FakeVoiceWorker(worked=False),
        processing_repository=repository,
        identity_repository=identity,
        notifier=notifier,
    )

    assert identity.locale_calls == [ACCESS]
    assert notifier.commands[0].locale is locale


@pytest.mark.asyncio
async def test_cycle_does_not_mark_notice_sent_when_telegram_fails() -> None:
    repository = FakeNoticeRepository(_notice_claim())

    with pytest.raises(RuntimeError):
        await process_access_once(
            access_context=ACCESS,
            now=NOW,
            worker=FakeVoiceWorker(worked=False),
            classification_worker=FakeVoiceWorker(worked=False),
            indexing_worker=FakeVoiceWorker(worked=False),
            processing_repository=repository,
            identity_repository=FakeWorkerIdentity(),
            notifier=FakeNotifier(RuntimeError("telegram unavailable")),
        )

    assert repository.sent == []


@pytest.mark.asyncio
async def test_cycle_reports_work_when_only_classification_processed() -> None:
    voice_worker = FakeVoiceWorker(worked=False)
    classification_worker = FakeVoiceWorker(worked=True)

    worked = await process_access_once(
        access_context=ACCESS,
        now=NOW,
        worker=voice_worker,
        classification_worker=classification_worker,
        indexing_worker=FakeVoiceWorker(worked=False),
        processing_repository=FakeNoticeRepository(None),
        identity_repository=FakeWorkerIdentity(),
        notifier=FakeNotifier(),
    )

    assert worked is True
    assert voice_worker.calls == [(ACCESS, NOW)]
    assert classification_worker.calls == [(ACCESS, NOW)]

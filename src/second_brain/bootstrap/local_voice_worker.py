import asyncio
from datetime import datetime
from typing import Protocol

from aiogram import Bot

from second_brain.bootstrap.classification_completion import (
    ClassificationCompletionInTransaction,
)
from second_brain.bootstrap.classification_source import (
    PostgresClassificationSourceReader,
)
from second_brain.bootstrap.classification_worker import ClassificationWorker
from second_brain.bootstrap.indexing_completion import IndexingCompletionInTransaction
from second_brain.bootstrap.indexing_source import PostgresIndexingSourceReader
from second_brain.bootstrap.indexing_worker import IndexingWorker
from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.voice_processing_completion import (
    VoiceDownloadCompletionInTransaction,
    VoiceTranscriptionCompletionInTransaction,
)
from second_brain.shared.clock import SystemClock
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresVoiceSourceRepository,
)
from second_brain.slices.classification.adapters.openrouter.model import (
    OpenRouterClassificationModel,
)
from second_brain.slices.classification.application.extraction import ClassifySource
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresWorkerIdentityRepository,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    WorkerIdentityPort,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.adapters.storage.local_voice_storage import (
    LocalVoiceStorage,
)
from second_brain.slices.processing.adapters.telegram.voice import (
    AiogramVoiceDownloader,
    AiogramVoiceNotifier,
)
from second_brain.slices.processing.adapters.transcription.mlx_whisper import (
    MlxWhisperTranscriptionModel,
)
from second_brain.slices.processing.application.contracts import (
    MarkProcessingNoticeSentCommand,
    SendProcessingNoticeCommand,
)
from second_brain.slices.processing.application.voice_worker import VoiceWorker
from second_brain.slices.processing.ports.repositories import ProcessingRepository
from second_brain.slices.processing.ports.voice import ProcessingNotifier
from second_brain.slices.retrieval.adapters.embedding.e5 import E5EmbeddingModel
from second_brain.slices.retrieval.application.indexing import IndexSource


class StepWorker(Protocol):
    async def process_once(
        self, access_context: AccessContext, now: datetime
    ) -> bool: ...


def build_classification_model(settings: Settings) -> OpenRouterClassificationModel:
    api_key = settings.open_router_ai_key
    if api_key is None:
        raise RuntimeError("OPEN_ROUTER_AI_KEY must be configured")
    return OpenRouterClassificationModel(api_key=api_key)


async def process_access_once(
    *,
    access_context: AccessContext,
    now: datetime,
    worker: StepWorker,
    classification_worker: StepWorker,
    indexing_worker: StepWorker,
    processing_repository: ProcessingRepository,
    identity_repository: WorkerIdentityPort,
    notifier: ProcessingNotifier,
) -> bool:
    worked = await worker.process_once(access_context, now)
    classified = await classification_worker.process_once(access_context, now)
    indexed = await indexing_worker.process_once(access_context, now)
    worked = worked or classified or indexed
    notice = await processing_repository.claim_due_notice(access_context, now)
    if notice is None:
        return worked
    recipient = await identity_repository.resolve_telegram_recipient(access_context)
    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=recipient.telegram_user_id,
            notice=notice,
        )
    )
    await processing_repository.mark_notice_sent(
        MarkProcessingNoticeSentCommand(
            access_context=access_context,
            notice_id=notice.notice_id,
            sent_at=now,
        )
    )
    return True


async def run_local_voice_worker(settings: Settings) -> None:
    classification_model = build_classification_model(settings)
    engine = create_database_engine(settings.database_url)
    bot: Bot | None = None
    try:
        await assert_non_privileged_application_role(engine)
        storage = LocalVoiceStorage(settings.voice_storage_root)
        await storage.prepare()
        transcription_model = MlxWhisperTranscriptionModel(settings.mlx_whisper_model)
        transcription_model.ensure_runtime()
        bot = Bot(settings.telegram_bot_token)
        session_factory = create_session_factory(engine)
        processing = PostgresProcessingRepository(session_factory)
        identities = PostgresWorkerIdentityRepository(session_factory)
        worker = VoiceWorker(
            queue=processing,
            voice_source=PostgresVoiceSourceRepository(session_factory),
            downloader=AiogramVoiceDownloader(bot),
            storage=storage,
            download_completion=VoiceDownloadCompletionInTransaction(session_factory),
            transcription_model=transcription_model,
            transcription_completion=VoiceTranscriptionCompletionInTransaction(
                session_factory
            ),
        )
        classification_worker = ClassificationWorker(
            queue=processing,
            source_reader=PostgresClassificationSourceReader(session_factory),
            classifier=ClassifySource(classification_model),
            completion=ClassificationCompletionInTransaction(session_factory),
        )
        # The E5 weights load lazily on the first indexing step, so the
        # embedding model is not a startup dependency of the process.
        indexing_worker = IndexingWorker(
            queue=processing,
            source_reader=PostgresIndexingSourceReader(session_factory),
            indexer=IndexSource(E5EmbeddingModel()),
            completion=IndexingCompletionInTransaction(session_factory),
        )
        notifier = AiogramVoiceNotifier(bot)
        clock = SystemClock()
        while True:
            worked = False
            for access_context in await identities.list_active_access_contexts():
                try:
                    processed = await process_access_once(
                        access_context=access_context,
                        now=clock.now(),
                        worker=worker,
                        classification_worker=classification_worker,
                        indexing_worker=indexing_worker,
                        processing_repository=processing,
                        identity_repository=identities,
                        notifier=notifier,
                    )
                    worked = processed or worked
                except Exception:
                    continue
            if not worked:
                await asyncio.sleep(1)
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run_local_voice_worker(Settings.from_environment()))


if __name__ == "__main__":
    main()

import asyncio
from datetime import datetime

from aiogram import Bot

from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.voice_processing_completion import (
    VoiceDownloadCompletionInTransaction,
    VoiceTranscriptionCompletionInTransaction,
)
from second_brain.shared.clock import SystemClock
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresVoiceSourceRepository,
)
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


async def process_access_once(
    *,
    access_context: AccessContext,
    now: datetime,
    worker: VoiceWorker,
    processing_repository: ProcessingRepository,
    identity_repository: WorkerIdentityPort,
    notifier: ProcessingNotifier,
) -> bool:
    worked = await worker.process_once(access_context, now)
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

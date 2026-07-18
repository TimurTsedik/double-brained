import asyncio

from aiogram import Bot

from second_brain.bootstrap.contact_intake_in_transaction import (
    ContactIntakeInTransaction,
)
from second_brain.bootstrap.digest_in_transaction import DigestInTransaction
from second_brain.bootstrap.exact_search_in_transaction import (
    ExactSearchInTransaction,
)
from second_brain.bootstrap.image_capture_in_transaction import (
    ImageCaptureInTransaction,
)
from second_brain.bootstrap.memory_ask_in_transaction import MemoryAskInTransaction
from second_brain.bootstrap.project_context_in_transaction import (
    ProjectContextInTransaction,
)
from second_brain.bootstrap.record_edit_in_transaction import RecordEditInTransaction
from second_brain.bootstrap.record_view_in_transaction import RecordViewInTransaction
from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.bootstrap.voice_capture_in_transaction import (
    VoiceCaptureInTransaction,
)
from second_brain.shared.clock import SystemClock
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresLocaleResolver,
    PostgresPanelContextResolver,
    PostgresPollerLock,
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.local_updates import LocalUpdateProcessor


async def run_local_polling(settings: Settings) -> None:
    engine = create_database_engine(settings.database_url)
    lock = PostgresPollerLock(engine)
    bot: Bot | None = None
    poller: LocalPoller | None = None
    try:
        await assert_non_privileged_application_role(engine)
        bot = Bot(settings.telegram_bot_token)
        bot_user = await bot.get_me()
        if bot_user.id is None:
            raise RuntimeError("Telegram bot identity did not include an id")
        session_factory = create_session_factory(engine)
        task_capture = TaskCaptureInTransaction()
        exact_search = ExactSearchInTransaction()
        project_context = ProjectContextInTransaction()
        # Один объект на оба порта показа: запись целиком + её sidecar-ссылки.
        record_view = RecordViewInTransaction(
            image_storage_root=settings.image_storage_root
        )
        processor = LocalUpdateProcessor(
            store=PostgresUpdateRepository(session_factory),
            clock=SystemClock(),
            pepper=settings.invite_token_pepper,
            pepper_key_id=settings.invite_token_pepper_key_id,
            capture_text_port=task_capture,
            task_mode_port=task_capture,
            task_panel_port=task_capture,
            exact_search_port=exact_search,
            capture_voice_port=VoiceCaptureInTransaction(),
            capture_image_port=ImageCaptureInTransaction(),
            project_panel_port=project_context,
            memory_ask_port=MemoryAskInTransaction(),
            bot_username=bot_user.username,
            reminder_ack_port=task_capture,
            contact_port=ContactIntakeInTransaction(),
            record_view_port=record_view,
            digest_port=DigestInTransaction(),
            record_links_port=record_view,
            record_edit_port=RecordEditInTransaction(),
        )
        poller = LocalPoller(
            AiogramGateway(
                bot,
                bot_user.id,
                PostgresLocaleResolver(session_factory),
                panel_context_resolver=PostgresPanelContextResolver(session_factory),
            ),
            processor,
            lock,
            panel_followup_seconds=settings.panel_followup_seconds,
        )
        while True:
            await poller.run_once()
            await asyncio.sleep(1)
    finally:
        if poller is not None:
            await poller.shutdown()
        await lock.close()
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run_local_polling(Settings.from_environment()))


if __name__ == "__main__":
    main()

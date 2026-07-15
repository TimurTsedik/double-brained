import asyncio

from aiogram import Bot

from second_brain.bootstrap.exact_search_in_transaction import (
    ExactSearchInTransaction,
)
from second_brain.bootstrap.memory_ask_in_transaction import MemoryAskInTransaction
from second_brain.bootstrap.project_context_in_transaction import (
    ProjectContextInTransaction,
)
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
            project_panel_port=project_context,
            memory_ask_port=MemoryAskInTransaction(),
        )
        poller = LocalPoller(AiogramGateway(bot, bot_user.id), processor, lock)
        while True:
            await poller.run_once()
            await asyncio.sleep(1)
    finally:
        await lock.close()
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run_local_polling(Settings.from_environment()))


if __name__ == "__main__":
    main()

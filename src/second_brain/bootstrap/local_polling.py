import asyncio

from aiogram import Bot

from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.update_processing import build_update_processor
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresLocaleResolver,
    PostgresPanelContextResolver,
    PostgresPollerLock,
)
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller


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
        # Композиция портов процессора — общая с inbox-шагом воркера
        # (webhook-путь): build_update_processor, пути не расходятся.
        processor = build_update_processor(session_factory, settings, bot_user.username)
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

import argparse
import asyncio
from collections.abc import Sequence

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import initialize_schema, reset_prototype_schema
from second_brain.bootstrap.settings import Settings
from second_brain.shared.clock import SystemClock
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresEnrollmentRepository,
)
from second_brain.slices.identity.application.enrollment import CreateEnrollmentInvite


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Identity bootstrap operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="create the prototype schema")
    reset = commands.add_parser(
        "reset-db", help="WARNING: drop and recreate the prototype schema"
    )
    reset.add_argument(
        "--confirm-prototype-reset",
        action="store_true",
        help="confirm the destructive prototype reset",
    )
    commands.add_parser(
        "create-bootstrap-admin-invite",
        help="print a one-time Telegram bootstrap-admin enrollment link",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, settings: Settings) -> None:
    database_url = (
        settings.schema_database_url
        if args.command in {"init-db", "reset-db"}
        else settings.database_url
    )
    engine = create_database_engine(database_url)
    try:
        if args.command == "init-db":
            await initialize_schema(engine)
            return
        if args.command == "reset-db":
            print("WARNING: resetting the prototype database destroys all data.")
            await reset_prototype_schema(engine, confirm=args.confirm_prototype_reset)
            return
        if args.command == "create-bootstrap-admin-invite":
            await assert_non_privileged_application_role(engine)
            await _print_invite(engine, settings)
            return
        raise RuntimeError("unknown identity command")
    finally:
        await engine.dispose()


async def _print_invite(engine: AsyncEngine, settings: Settings) -> None:
    bot = Bot(settings.telegram_bot_token)
    try:
        bot_user = await bot.get_me()
    finally:
        await bot.session.close()
    if bot_user.username is None:
        raise RuntimeError("the Telegram bot must have a username for enrollment links")
    repository = PostgresEnrollmentRepository(create_session_factory(engine))
    invite = await CreateEnrollmentInvite(
        repository,
        SystemClock(),
        settings.invite_token_pepper,
        settings.invite_token_pepper_key_id,
    ).execute()
    print(f"https://t.me/{bot_user.username}?start={invite.token}")


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(run(parse_args(argv), Settings.from_environment()))


if __name__ == "__main__":
    main()

"""Приглашение из бота: кнопка (только админ), callback invite:create, доставка
ссылки как side-effect, и «admin — не суперпользователь» (изоляция от member)."""

from datetime import UTC, datetime
from hmac import digest
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import DEFAULT_LOCALE, Locale, resolve_locale
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    EnrollmentInvite,
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresPanelContextResolver,
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram import (
    messages,
)
from second_brain.slices.identity.adapters.telegram import (
    poller as poller_mod,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from tests.identity.conftest import IsolatedDatabase
from tests.identity.locale_fakes import FakeLocaleResolver, FakePanelContextResolver

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PEPPER = b"invite-flow-pepper"
PEPPER_KEY_ID = "invite-key"
BOT_USERNAME = "second_brain_bot"


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_invite_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


async def seed_user(
    schema_engine: AsyncEngine,
    *,
    role: str,
    telegram_user_id: int,
    language: str | None = "ru",
    user_active: bool = True,
) -> AccessContext:
    user_id = uuid4()
    space_id = uuid4()
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            session.add(
                User(
                    id=user_id,
                    role=role,
                    is_active=user_active,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add_all(
                [
                    UserSpace(
                        id=space_id,
                        owner_user_id=user_id,
                        timezone="Asia/Jerusalem",
                        language=language,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    TelegramIdentity(
                        id=uuid4(),
                        telegram_user_id=telegram_user_id,
                        user_id=user_id,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
    return AccessContext(user_id=user_id, user_space_id=space_id)


def build_processor(
    engine: AsyncEngine, bot_username: str | None = BOT_USERNAME
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=PEPPER,
        pepper_key_id=PEPPER_KEY_ID,
        bot_username=bot_username,
    )


def invite_callback(update_id: int, telegram_user_id: int) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        telegram_user_id,
        None,
        callback_query_id=f"cb-{update_id}",
        callback_data="invite:create",
    )


async def pending_member_invites(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(EnrollmentInvite)
            .where(EnrollmentInvite.status == "pending")
        )
        or 0
    )


async def total_invites(session: AsyncSession) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(EnrollmentInvite)) or 0
    )


async def seed_pending_member_invite(schema_engine: AsyncEngine, token: str) -> None:
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            session.add(
                EnrollmentInvite(
                    id=uuid4(),
                    token_hash=digest(PEPPER, token.encode(), "sha256"),
                    pepper_key_id=PEPPER_KEY_ID,
                    role="member",
                    status="pending",
                    created_by_actor="admin_bot",
                    created_at=NOW,
                    expires_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
                )
            )


def start_update(update_id: int, telegram_user_id: int, token: str) -> TelegramUpdate:
    return TelegramUpdate(1, update_id, True, telegram_user_id, f"/start {token}")


# ---------------------------------------------------------------------------
# processor: invite:create authorization + link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_invite_create_makes_member_invite_and_returns_link(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=42)

    result = await build_processor(engine).process(invite_callback(1, 42))

    invite = await session.scalar(select(EnrollmentInvite))
    assert result.kind is AcknowledgementKind.INVITE_CREATED
    assert result.invite_link is not None
    assert result.invite_link.startswith(f"https://t.me/{BOT_USERNAME}?start=")
    assert invite is not None
    assert invite.role == "member"
    assert invite.created_by_actor == "admin_bot"
    assert invite.status == "pending"
    # Ссылка несёт plaintext-токен, чей хэш и лежит в БД.
    token = result.invite_link.rsplit("=", 1)[1]
    assert invite.token_hash == digest(PEPPER, token.encode(), "sha256")


@pytest.mark.asyncio
async def test_active_member_with_language_is_forbidden_and_writes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="member", telegram_user_id=77, language="ru")

    result = await build_processor(engine).process(invite_callback(1, 77))

    assert result.kind is AcknowledgementKind.INVITE_FORBIDDEN
    assert result.invite_link is None
    assert await total_invites(session) == 0


@pytest.mark.asyncio
async def test_active_member_without_language_is_prompted_and_writes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="member", telegram_user_id=88, language=None)

    result = await build_processor(engine).process(invite_callback(1, 88))

    # NULL-язык: сначала выбор языка, invite не создаётся.
    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
    assert await total_invites(session) == 0


@pytest.mark.asyncio
async def test_inactive_admin_invite_create_writes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=99, user_active=False)

    result = await build_processor(engine).process(invite_callback(1, 99))

    # Неактивный → access_context is None → IGNORED (не выдаём кнопку), 0 строк.
    assert result.kind is AcknowledgementKind.IGNORED
    assert await total_invites(session) == 0


@pytest.mark.asyncio
async def test_unknown_actor_invite_create_writes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    result = await build_processor(engine).process(invite_callback(1, 404))

    # Незнакомец → IGNORED (как любой callback от чужака), ни одной строки invite.
    assert result.kind is AcknowledgementKind.IGNORED
    assert await total_invites(session) == 0


@pytest.mark.asyncio
async def test_missing_bot_username_creates_no_invite(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=42)

    result = await build_processor(engine, bot_username=None).process(
        invite_callback(1, 42)
    )

    assert result.kind is AcknowledgementKind.INVITE_FORBIDDEN
    assert await total_invites(session) == 0


@pytest.mark.asyncio
async def test_empty_bot_username_creates_no_invite(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=42)

    result = await build_processor(engine, bot_username="").process(
        invite_callback(1, 42)
    )

    assert result.kind is AcknowledgementKind.INVITE_FORBIDDEN
    assert await total_invites(session) == 0


@pytest.mark.asyncio
async def test_known_user_reopening_invite_gets_welcome_back_without_consuming(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Уже подключённый (telegram 42 = admin) повторно открывает ЧУЖОЙ pending
    # member-invite: видит «С возвращением», invite остаётся pending для того,
    # кому предназначен, без нового User/space/identity.
    await seed_user(schema_engine, role="admin", telegram_user_id=42)
    await seed_pending_member_invite(schema_engine, token="intended-recipient-token")

    result = await build_processor(engine).process(
        start_update(1, 42, "intended-recipient-token")
    )

    invite = await session.scalar(select(EnrollmentInvite))
    assert result.kind is AcknowledgementKind.KNOWN_USER_STARTED
    assert invite is not None and invite.status == "pending"
    assert int(await session.scalar(select(func.count()).select_from(User)) or 0) == 1
    assert (
        int(await session.scalar(select(func.count()).select_from(UserSpace)) or 0) == 1
    )
    assert (
        int(
            await session.scalar(select(func.count()).select_from(TelegramIdentity))
            or 0
        )
        == 1
    )


# ---------------------------------------------------------------------------
# gateway: role-aware panel + invite message
# ---------------------------------------------------------------------------


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def gateway_for(is_admin: bool) -> tuple[RecordingBot, AiogramGateway]:
    bot = RecordingBot()
    gateway = AiogramGateway(
        cast(Bot, bot),
        bot_id=1,
        locale_resolver=FakeLocaleResolver(Locale.RU),
        panel_context_resolver=FakePanelContextResolver(Locale.RU, is_admin=is_admin),
    )
    return bot, gateway


def _panel_update() -> TelegramUpdate:
    return TelegramUpdate(1, 1, True, 42, "/start")


@pytest.mark.asyncio
async def test_admin_panel_shows_invite_button() -> None:
    bot, gateway = gateway_for(is_admin=True)

    await gateway.send_panel(_panel_update())

    markup = bot.sent[0]["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "invite:create" in callbacks


@pytest.mark.asyncio
async def test_member_panel_hides_invite_button() -> None:
    bot, gateway = gateway_for(is_admin=False)

    await gateway.send_panel(_panel_update())

    markup = bot.sent[0]["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "invite:create" not in callbacks


@pytest.mark.asyncio
async def test_invite_message_carries_link_in_both_locales() -> None:
    link = "https://t.me/second_brain_bot?start=abc"
    bot_ru, gateway_ru = gateway_for(is_admin=True)
    await gateway_ru.send_invite_link(_panel_update(), link)
    assert link in bot_ru.sent[0]["text"]
    assert bot_ru.sent[0]["text"] == messages.invite_message_text(link, Locale.RU)

    bot = RecordingBot()
    gateway_en = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver(Locale.EN)
    )
    await gateway_en.send_invite_link(_panel_update(), link)
    assert bot.sent[0]["text"] == messages.invite_message_text(link, Locale.EN)
    assert link in bot.sent[0]["text"]


# ---------------------------------------------------------------------------
# efficiency: panel resolves locale + is_admin in ONE round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_context_resolver_returns_locale_and_role_together(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=201, language="en")
    await seed_user(schema_engine, role="member", telegram_user_id=202, language="ru")
    resolver = PostgresPanelContextResolver(create_session_factory(engine))

    admin_ctx = await resolver.resolve_panel_context(201)
    member_ctx = await resolver.resolve_panel_context(202)
    unknown_ctx = await resolver.resolve_panel_context(999)

    assert (admin_ctx.locale, admin_ctx.is_admin) == (Locale.EN, True)
    assert (member_ctx.locale, member_ctx.is_admin) == (Locale.RU, False)
    # Неизвестный → тот же fallback, что и раньше.
    assert unknown_ctx.locale == resolve_locale(None)
    assert unknown_ctx.locale == DEFAULT_LOCALE
    assert unknown_ctx.is_admin is False


@pytest.mark.asyncio
async def test_send_panel_via_combined_resolver_shows_invite_for_admin(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=42, language="ru")
    bot = RecordingBot()
    gateway = AiogramGateway(
        cast(Bot, bot),
        bot_id=1,
        locale_resolver=FakeLocaleResolver(Locale.RU),
        panel_context_resolver=PostgresPanelContextResolver(
            create_session_factory(engine)
        ),
    )

    await gateway.send_panel(TelegramUpdate(1, 1, True, 42, "/start"))

    markup = bot.sent[0]["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "invite:create" in callbacks


# ---------------------------------------------------------------------------
# poller dispatch of the invite link side-effect
# ---------------------------------------------------------------------------


class _InviteSpyGateway:
    bot_id = 1

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.links: list[str] = []

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        if offset is not None:
            return []
        return [invite_callback(1, 42)]

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.calls.append("answer_callback")

    async def send_invite_link(self, update: TelegramUpdate, link: str) -> None:
        self.calls.append("send_invite_link")
        self.links.append(link)

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append("send_acknowledgement")


class _ResultProcessor:
    def __init__(self, kind: AcknowledgementKind, invite_link: str | None) -> None:
        self._kind = kind
        self._invite_link = invite_link

    async def process(self, update: TelegramUpdate) -> Any:
        return type(
            "R",
            (),
            {"kind": self._kind, "fresh": True, "invite_link": self._invite_link},
        )()


class _AlwaysLock:
    async def acquire(self, bot_id: int) -> bool:
        return True


@pytest.mark.asyncio
async def test_poller_dispatches_invite_link_and_not_acknowledgement() -> None:
    gateway = _InviteSpyGateway()
    link = "https://t.me/second_brain_bot?start=xyz"
    poller = poller_mod.LocalPoller(
        gateway,  # type: ignore[arg-type]
        _ResultProcessor(AcknowledgementKind.INVITE_CREATED, link),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert gateway.calls.count("send_invite_link") == 1
    assert gateway.links == [link]
    assert "send_acknowledgement" not in gateway.calls


@pytest.mark.asyncio
async def test_poller_stays_silent_on_invite_forbidden() -> None:
    gateway = _InviteSpyGateway()
    poller = poller_mod.LocalPoller(
        gateway,  # type: ignore[arg-type]
        _ResultProcessor(AcknowledgementKind.INVITE_FORBIDDEN, None),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert "send_invite_link" not in gateway.calls
    assert "send_acknowledgement" not in gateway.calls


# ---------------------------------------------------------------------------
# M8: admin is NOT a superuser — cannot read a member's capture
# ---------------------------------------------------------------------------


def capture_command(
    access: AccessContext, update_id: int, text: str
) -> CaptureTextCommand:
    return CaptureTextCommand(
        access_context=access,
        bot_id=100,
        telegram_update_id=update_id,
        telegram_message_id=update_id + 1000,
        raw_text=text,
        received_at=NOW,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
async def test_admin_cannot_read_a_members_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    admin = await seed_user(schema_engine, role="admin", telegram_user_id=1001)
    member = await seed_user(schema_engine, role="member", telegram_user_id=1002)
    repository = PostgresCaptureEventRepository(create_session_factory(engine))

    admin_event = await repository.create(capture_command(admin, 10, "admin note"))
    await repository.create(capture_command(member, 11, "member secret"))

    # Каждый видит ТОЛЬКО своё пространство: admin не читает запись member'а и
    # наоборот. Роль admin не даёт сквозного доступа (RLS по user_space_id).
    assert await repository.list_recent(admin) == [admin_event]
    assert await repository.count(admin) == 1
    assert await repository.count(member) == 1
    member_events = await repository.list_recent(member)
    assert admin_event not in member_events

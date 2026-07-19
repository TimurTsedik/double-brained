"""Кнопка «🔑 API» в боте: выдать / посмотреть / отозвать свои токены.

Форма потока — та же, что у «➕ Пригласить» (панель → callback → application →
transient-payload → презентер → каталог строк), с одним отличием: кнопка видна
КАЖДОМУ пользователю, потому что токен даёт доступ к его собственной памяти.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    ApiToken,
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram import messages
from second_brain.slices.identity.adapters.telegram import poller as poller_mod
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.identity.ports.repositories import ApiTokenView
from tests.identity.conftest import IsolatedDatabase
from tests.identity.locale_fakes import FakeLocaleResolver, FakePanelContextResolver

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
INVITE_PEPPER = b"invite-pepper"
API_PEPPER = b"api-pepper"
API_PEPPER_KEY_ID = "api-key-v1"


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def reset_api_bot_schema(
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
    engine: AsyncEngine, *, api_pepper: bytes | None = API_PEPPER
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=INVITE_PEPPER,
        pepper_key_id="invite-key",
        bot_username="second_brain_bot",
        api_token_pepper=api_pepper,
        api_token_pepper_key_id=API_PEPPER_KEY_ID,
    )


def api_callback(update_id: int, telegram_user_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        telegram_user_id,
        None,
        callback_query_id=f"cb-{update_id}",
        callback_data=data,
    )


async def total_tokens(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(ApiToken)) or 0)


# ---------------------------------------------------------------------------
# processor: create / list / revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_creates_a_token_and_sees_the_secret_once(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # В отличие от приглашения, кнопка доступна не только админу.
    await seed_user(schema_engine, role="member", telegram_user_id=77)

    result = await build_processor(engine).process(api_callback(1, 77, "api:create"))

    stored = await session.scalar(select(ApiToken))
    assert result.kind is AcknowledgementKind.API_TOKEN_CREATED
    assert result.api_token_secret
    assert stored is not None
    assert stored.revoked_at is None
    # Секрет — transient-payload: в repr результата его быть не должно.
    assert result.api_token_secret not in repr(result)
    # Список для показа рядом с секретом не нужен, метка — нужна.
    assert result.api_token_label == stored.label


@pytest.mark.asyncio
async def test_menu_lists_own_tokens_only(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=42)
    await seed_user(schema_engine, role="member", telegram_user_id=77)
    processor = build_processor(engine)
    await processor.process(api_callback(1, 42, "api:create"))
    await processor.process(api_callback(2, 77, "api:create"))

    result = await processor.process(api_callback(3, 42, "api:menu"))

    assert result.kind is AcknowledgementKind.API_TOKENS_LISTED
    assert result.api_tokens is not None
    assert len(result.api_tokens) == 1


@pytest.mark.asyncio
async def test_revoke_marks_the_token_and_returns_the_fresh_list(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="member", telegram_user_id=77)
    processor = build_processor(engine)
    created = await processor.process(api_callback(1, 77, "api:create"))
    token = await session.scalar(select(ApiToken))
    assert token is not None and created.api_token_secret

    result = await processor.process(api_callback(2, 77, f"api:revoke:{token.id}"))

    await session.refresh(token)
    assert result.kind is AcknowledgementKind.API_TOKEN_REVOKED
    assert token.revoked_at == NOW
    assert result.api_tokens is not None
    assert [view.revoked_at is not None for view in result.api_tokens] == [True]


@pytest.mark.asyncio
async def test_revoking_a_foreign_token_writes_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="admin", telegram_user_id=42)
    await seed_user(schema_engine, role="member", telegram_user_id=77)
    processor = build_processor(engine)
    await processor.process(api_callback(1, 42, "api:create"))
    token = await session.scalar(select(ApiToken))
    assert token is not None

    result = await processor.process(api_callback(2, 77, f"api:revoke:{token.id}"))

    await session.refresh(token)
    # Чужой uuid неотличим от мусорного callback'а: ни сообщения, ни правки.
    assert result.kind is AcknowledgementKind.IGNORED
    assert token.revoked_at is None


@pytest.mark.asyncio
async def test_unknown_actor_and_malformed_callbacks_write_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="member", telegram_user_id=77)
    processor = build_processor(engine)

    stranger = await processor.process(api_callback(1, 404, "api:create"))
    garbage = await processor.process(api_callback(2, 77, "api:revoke:not-a-uuid"))
    unknown = await processor.process(api_callback(3, 77, "api:drop-everything"))

    assert stranger.kind is AcknowledgementKind.IGNORED
    assert garbage.kind is AcknowledgementKind.IGNORED
    assert unknown.kind is AcknowledgementKind.IGNORED
    assert await total_tokens(session) == 0


@pytest.mark.asyncio
async def test_language_is_asked_before_any_token_is_issued(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    await seed_user(schema_engine, role="member", telegram_user_id=88, language=None)

    result = await build_processor(engine).process(api_callback(1, 88, "api:create"))

    assert result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
    assert await total_tokens(session) == 0


@pytest.mark.asyncio
async def test_missing_api_pepper_creates_no_token(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Перец не подключён → механизма нет; молчим, а не пишем нерабочую строку.
    await seed_user(schema_engine, role="member", telegram_user_id=77)

    result = await build_processor(engine, api_pepper=None).process(
        api_callback(1, 77, "api:create")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert await total_tokens(session) == 0


# ---------------------------------------------------------------------------
# gateway: панель у всех, тексты на двух языках
# ---------------------------------------------------------------------------


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def gateway_for(
    locale: Locale, is_admin: bool = False
) -> tuple[RecordingBot, AiogramGateway]:
    bot = RecordingBot()
    gateway = AiogramGateway(
        cast(Bot, bot),
        bot_id=1,
        locale_resolver=FakeLocaleResolver(locale),
        panel_context_resolver=FakePanelContextResolver(locale, is_admin=is_admin),
    )
    return bot, gateway


def _panel_update() -> TelegramUpdate:
    return TelegramUpdate(1, 1, True, 42, "/start")


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [True, False])
async def test_panel_shows_the_api_button_to_everyone(is_admin: bool) -> None:
    bot, gateway = gateway_for(Locale.RU, is_admin=is_admin)

    await gateway.send_panel(_panel_update())

    markup = bot.sent[0]["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "api:menu" in callbacks


@pytest.mark.asyncio
async def test_created_message_warns_and_carries_the_secret_in_both_locales() -> None:
    secret = "s3cr3t-token-value"
    for locale in Locale:
        bot, gateway = gateway_for(locale)
        await gateway.send_api_token_created(_panel_update(), "api-1", secret)
        text = bot.sent[0]["text"]
        assert text == messages.api_token_created_text("api-1", secret, locale)
        assert secret in text
    ru = messages.api_token_created_text("api-1", secret, Locale.RU)
    en = messages.api_token_created_text("api-1", secret, Locale.EN)
    assert ru != en
    # Честное предупреждение: один раз, хранить надёжно, сообщение удалить.
    assert "один раз" in ru.lower()
    assert "once" in en.lower()


@pytest.mark.asyncio
async def test_token_panel_renders_state_and_revoke_buttons() -> None:
    tokens = (
        ApiTokenView(
            id=uuid4(),
            label="api-1",
            created_at=NOW,
            last_used_at=NOW + timedelta(hours=2),
            revoked_at=None,
        ),
        ApiTokenView(
            id=uuid4(),
            label="api-2",
            created_at=NOW,
            last_used_at=None,
            revoked_at=NOW,
        ),
    )
    bot, gateway = gateway_for(Locale.RU)

    await gateway.send_api_token_panel(_panel_update(), tokens)

    message = bot.sent[0]
    assert "api-1" in message["text"] and "api-2" in message["text"]
    callbacks = [
        b.callback_data for row in message["reply_markup"].inline_keyboard for b in row
    ]
    # Выдать может каждый; отозвать — только живой токен.
    assert callbacks == ["api:create", f"api:revoke:{tokens[0].id}"]


@pytest.mark.asyncio
async def test_empty_token_panel_is_honest_in_both_locales() -> None:
    for locale in Locale:
        bot, gateway = gateway_for(locale)
        await gateway.send_api_token_panel(_panel_update(), ())
        assert bot.sent[0]["text"] == messages.api_token_panel_empty(locale)
    assert messages.api_token_panel_empty(Locale.RU) != messages.api_token_panel_empty(
        Locale.EN
    )


# ---------------------------------------------------------------------------
# презентер: какой вид результата во что превращается
# ---------------------------------------------------------------------------


class _ApiSpyGateway:
    bot_id = 1

    def __init__(self, callback_data: str) -> None:
        self._callback_data = callback_data
        self.calls: list[str] = []
        self.secrets: list[str] = []

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        if offset is not None:
            return []
        return [api_callback(1, 42, self._callback_data)]

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.calls.append("answer_callback")

    async def send_api_token_created(
        self, update: TelegramUpdate, label: str, secret: str
    ) -> None:
        self.calls.append("send_api_token_created")
        self.secrets.append(secret)

    async def send_api_token_panel(
        self, update: TelegramUpdate, tokens: tuple[ApiTokenView, ...]
    ) -> None:
        self.calls.append("send_api_token_panel")

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append("send_acknowledgement")


class _ResultProcessor:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def process(self, update: TelegramUpdate) -> Any:
        return self._result


class _AlwaysLock:
    async def acquire(self, bot_id: int) -> bool:
        return True


def _result(kind: AcknowledgementKind, **fields: Any) -> Any:
    payload = {
        "kind": kind,
        "fresh": True,
        "api_token_secret": None,
        "api_token_label": None,
        "api_tokens": None,
        **fields,
    }
    return type("R", (), payload)()


@pytest.mark.asyncio
async def test_poller_sends_the_secret_and_no_generic_acknowledgement() -> None:
    gateway = _ApiSpyGateway("api:create")
    poller = poller_mod.LocalPoller(
        gateway,  # type: ignore[arg-type]
        _ResultProcessor(
            _result(
                AcknowledgementKind.API_TOKEN_CREATED,
                api_token_secret="secret-value",
                api_token_label="api-1",
            )
        ),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert gateway.calls.count("send_api_token_created") == 1
    assert gateway.secrets == ["secret-value"]
    assert "send_acknowledgement" not in gateway.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [AcknowledgementKind.API_TOKENS_LISTED, AcknowledgementKind.API_TOKEN_REVOKED],
)
async def test_poller_shows_the_panel_for_list_and_revoke(
    kind: AcknowledgementKind,
) -> None:
    gateway = _ApiSpyGateway("api:menu")
    poller = poller_mod.LocalPoller(
        gateway,  # type: ignore[arg-type]
        _ResultProcessor(_result(kind, api_tokens=())),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert gateway.calls.count("send_api_token_panel") == 1
    assert "send_acknowledgement" not in gateway.calls

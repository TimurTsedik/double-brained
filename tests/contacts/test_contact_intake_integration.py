from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.contact_intake_in_transaction import (
    ContactIntakeInTransaction,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
from second_brain.slices.contacts.adapters.persistence.models import ContactModel
from second_brain.slices.contacts.application.contracts import (
    SaveContactCommand,
    TelegramContactPayload,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    TelegramUpdateReceipt,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram import messages
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
    UpdateResult,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_contact_schema(
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
                language="ru",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(TelegramIdentity).values(
                id=UUID("00000000-0000-0000-0000-000000000021"),
                telegram_user_id=42,
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


class FixedClock:
    def now(self) -> datetime:
        return NOW


def real_processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        FixedClock(),
        b"test-pepper",
        "test-key",
        contact_port=ContactIntakeInTransaction(),
    )


def contact_update(
    update_id: int,
    *,
    telegram_user_id: int = 42,
    first_name: str = "Ави",
    last_name: str | None = None,
    phone_number: str = "+972-50-111-22-33",
    is_private: bool = True,
) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=is_private,
        telegram_user_id=telegram_user_id,
        text=None,
        telegram_message_id=update_id + 1_000,
        contact=TelegramContactPayload(
            phone_number=phone_number,
            first_name=first_name,
            last_name=last_name,
        ),
    )


async def stored_contacts(schema_engine: AsyncEngine) -> list[ContactModel]:
    # Читаем владельцем схемы (мимо RLS): проверяем именно то, что легло в базу.
    async with create_session_factory(schema_engine)() as session:
        return list(await session.scalars(select(ContactModel)))


@pytest.mark.asyncio
async def test_contact_from_enrolled_sender_is_saved_and_acknowledged(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    result = await real_processor(engine).process(
        contact_update(500, first_name="Ави", last_name="Коэн")
    )

    assert result.kind is AcknowledgementKind.CONTACT_SAVED
    assert result.fresh is True
    assert result.contact_name == "Ави Коэн"
    contacts = await stored_contacts(schema_engine)
    assert len(contacts) == 1
    assert contacts[0].user_space_id == ACCESS.user_space_id
    assert contacts[0].display_name == "Ави Коэн"
    assert contacts[0].phone_number == "+972-50-111-22-33"
    receipt = await session.scalar(select(TelegramUpdateReceipt))
    assert receipt is not None
    assert receipt.result_kind == "contact_saved"


@pytest.mark.asyncio
async def test_resharing_the_same_name_updates_the_phone_without_a_duplicate(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    processor = real_processor(engine)
    await processor.process(contact_update(510, phone_number="+972-50-111-22-33"))

    # Повторная карточка с тем же именем в ДРУГОМ регистре: lower(display_name)
    # уникален — обновляется номер, дубль не появляется.
    result = await processor.process(
        contact_update(511, first_name="АВИ", phone_number="+972-50-999-88-77")
    )

    assert result.kind is AcknowledgementKind.CONTACT_SAVED
    contacts = await stored_contacts(schema_engine)
    assert len(contacts) == 1
    assert contacts[0].display_name == "Ави"
    assert contacts[0].phone_number == "+972-50-999-88-77"


@pytest.mark.asyncio
async def test_contact_from_a_stranger_is_ignored_and_nothing_is_written(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    result = await real_processor(engine).process(
        contact_update(520, telegram_user_id=404)
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.contact_name is None
    assert await stored_contacts(schema_engine) == []


@pytest.mark.asyncio
async def test_non_private_contact_is_ignored(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    result = await real_processor(engine).process(contact_update(525, is_private=False))

    assert result.kind is AcknowledgementKind.IGNORED
    assert await stored_contacts(schema_engine) == []


@pytest.mark.asyncio
async def test_replayed_update_keeps_one_contact_and_returns_no_transient_name(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    processor = real_processor(engine)
    update = contact_update(530)

    first = await processor.process(update)
    second = await processor.process(update)

    assert first.kind is AcknowledgementKind.CONTACT_SAVED
    assert second.kind is AcknowledgementKind.CONTACT_SAVED
    assert second.fresh is False
    assert second.contact_name is None
    assert len(await stored_contacts(schema_engine)) == 1


def test_contact_payload_and_command_hide_pii_from_repr() -> None:
    payload = TelegramContactPayload(
        phone_number="+972-50-111-22-33", first_name="Ави", last_name="Коэн"
    )
    command = SaveContactCommand(
        access_context=ACCESS,
        display_name="Ави Коэн",
        phone_number="+972-50-111-22-33",
        saved_at=NOW,
        trace_id="1" * 32,
    )
    update = contact_update(540, first_name="Ави", last_name="Коэн")
    result = UpdateResult(
        AcknowledgementKind.CONTACT_SAVED,
        "1" * 32,
        "2" * 16,
        fresh=True,
        contact_name="Ави Коэн",
    )

    for value in (repr(payload), repr(command), repr(update), repr(result)):
        assert "Ави" not in value
        assert "Коэн" not in value
        assert "+972-50-111-22-33" not in value


# ---------------------------------------------------------------------------
# poller dispatch: fresh-only ack with the transient {name} payload
# ---------------------------------------------------------------------------


class _SpyGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.calls: list[tuple[str, Any]] = []

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        if offset is not None:
            return []
        return [self._update]

    async def send_contact_saved(self, update: TelegramUpdate, name: str) -> None:
        self.calls.append(("send_contact_saved", name))

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append(("send_acknowledgement", kind))


class _AlwaysLock:
    async def acquire(self, bot_id: int) -> bool:
        return True


class _ContactSavedProcessor:
    def __init__(self, fresh: bool) -> None:
        self._fresh = fresh

    async def process(self, update: TelegramUpdate) -> UpdateResult:
        return UpdateResult(
            AcknowledgementKind.CONTACT_SAVED,
            "1" * 32,
            "2" * 16,
            fresh=self._fresh,
            contact_name="Ави" if self._fresh else None,
        )


@pytest.mark.asyncio
async def test_poller_sends_the_contact_saved_ack_with_the_name_once() -> None:
    gateway = _SpyGateway(contact_update(550))
    poller = LocalPoller(
        gateway,  # type: ignore[arg-type]
        _ContactSavedProcessor(fresh=True),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert gateway.calls == [("send_contact_saved", "Ави")]


@pytest.mark.asyncio
async def test_poller_stays_silent_on_a_duplicate_contact_update() -> None:
    gateway = _SpyGateway(contact_update(551))
    poller = LocalPoller(
        gateway,  # type: ignore[arg-type]
        _ContactSavedProcessor(fresh=False),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert gateway.calls == []


def test_contact_saved_message_is_localized_in_both_languages() -> None:
    assert messages.contact_saved_text("Ави", Locale.RU) == "📇 Контакт сохранён: Ави"
    assert messages.contact_saved_text("Avi", Locale.EN) == "📇 Contact saved: Avi"

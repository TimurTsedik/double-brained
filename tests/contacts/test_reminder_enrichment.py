from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.reminder_delivery import ReminderDeliveryStep
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventRepository,
)
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.contacts.adapters.persistence.repository import (
    PostgresContactWriter,
)
from second_brain.slices.contacts.application.contracts import SaveContactCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.adapters.persistence.repository import (
    PostgresReminderRepository,
)
from second_brain.slices.reminders.application.contracts import CreateReminderCommand
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresTaskRepository,
)
from second_brain.slices.tasks.application.contracts import CreateTaskCommand
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000012"),
)


class SpyReminderDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []

    async def deliver(self, text: str, recipient: TelegramRecipient) -> int:
        self.sent.append((text, recipient.telegram_user_id))
        return 777_000 + len(self.sent)


class FixedWorkerIdentity:
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        return (ACCESS_A,)

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        return TelegramRecipient(telegram_user_id=42)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        return Locale.RU


@pytest_asyncio.fixture(autouse=True)
async def reset_enrichment_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                {
                    "id": access.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS_A, ACCESS_B)
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": access.user_space_id,
                    "owner_user_id": access.user_id,
                    "timezone": "Asia/Jerusalem",
                    "language": "ru",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS_A, ACCESS_B)
            ],
        )


async def seed_reminder(
    engine: AsyncEngine,
    access_context: AccessContext,
    *,
    title: str,
    update_id: int,
) -> None:
    session_factory = create_session_factory(engine)
    source = await PostgresCaptureEventRepository(session_factory).create(
        CaptureTextCommand(
            access_context=access_context,
            bot_id=100,
            telegram_update_id=update_id,
            telegram_message_id=update_id + 1_000,
            raw_text=title,
            received_at=NOW,
            trace_id="1" * 32,
        )
    )
    task = await PostgresTaskRepository(session_factory).create(
        CreateTaskCommand(
            access_context=access_context,
            title=title,
            source_capture_event_id=source.id,
            created_at=NOW,
            trace_id="1" * 32,
        )
    )
    await PostgresReminderRepository(session_factory).create_reminder(
        CreateReminderCommand(
            access_context=access_context,
            remind_at=NOW - timedelta(minutes=1),
            text=title,
            source_task_id=task.id,
            created_at=NOW,
            trace_id="1" * 32,
        )
    )


async def seed_contact(
    engine: AsyncEngine,
    access_context: AccessContext,
    *,
    name: str,
    phone: str,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await PostgresContactWriter(session).upsert_contact(
            SaveContactCommand(
                access_context=access_context,
                display_name=name,
                phone_number=phone,
                saved_at=NOW,
                trace_id="1" * 32,
            )
        )


def delivery_step(
    engine: AsyncEngine, spy: SpyReminderDelivery
) -> ReminderDeliveryStep:
    return ReminderDeliveryStep(
        create_session_factory(engine), spy, FixedWorkerIdentity()
    )


@pytest.mark.asyncio
async def test_reminder_with_a_known_name_is_delivered_with_the_phone(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-50-111-22-33")
    await seed_reminder(engine, ACCESS_A, title="позвонить Ави", update_id=600)
    spy = SpyReminderDelivery()

    worked = await delivery_step(engine, spy).process_once(ACCESS_A, NOW)

    assert worked is True
    assert spy.sent == [("⏰ Напоминание: позвонить Ави · +972-50-111-22-33", 42)]
    # Номер живёт только в доставленном тексте — в строку напоминания он
    # НЕ сохраняется.
    async with create_session_factory(schema_engine)() as session:
        stored_text = await session.scalar(select(ReminderModel.text))
    assert stored_text == "позвонить Ави"


@pytest.mark.asyncio
async def test_matching_at_delivery_is_case_insensitive(
    engine: AsyncEngine,
) -> None:
    await seed_contact(engine, ACCESS_A, name="ави", phone="+972-50-111-22-33")
    await seed_reminder(engine, ACCESS_A, title="позвонить АВИ", update_id=610)
    spy = SpyReminderDelivery()

    await delivery_step(engine, spy).process_once(ACCESS_A, NOW)

    assert spy.sent == [("⏰ Напоминание: позвонить АВИ · +972-50-111-22-33", 42)]


@pytest.mark.asyncio
async def test_name_inside_another_word_does_not_enrich_the_delivery(
    engine: AsyncEngine,
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-50-111-22-33")
    await seed_reminder(engine, ACCESS_A, title="доставить правила", update_id=620)
    spy = SpyReminderDelivery()

    await delivery_step(engine, spy).process_once(ACCESS_A, NOW)

    assert spy.sent == [("⏰ Напоминание: доставить правила", 42)]


@pytest.mark.asyncio
async def test_two_known_names_deliver_both_phones(
    engine: AsyncEngine,
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-50-111-22-33")
    await seed_contact(engine, ACCESS_A, name="Маше", phone="+972-50-444-55-66")
    await seed_reminder(engine, ACCESS_A, title="позвонить Ави и Маше", update_id=630)
    spy = SpyReminderDelivery()

    await delivery_step(engine, spy).process_once(ACCESS_A, NOW)

    assert spy.sent == [
        (
            "⏰ Напоминание: позвонить Ави и Маше"
            " · +972-50-111-22-33 · +972-50-444-55-66",
            42,
        )
    ]


@pytest.mark.asyncio
async def test_no_known_name_leaves_the_delivered_text_unchanged(
    engine: AsyncEngine,
) -> None:
    await seed_contact(engine, ACCESS_A, name="Ави", phone="+972-50-111-22-33")
    await seed_reminder(engine, ACCESS_A, title="купить хлеб", update_id=640)
    spy = SpyReminderDelivery()

    await delivery_step(engine, spy).process_once(ACCESS_A, NOW)

    assert spy.sent == [("⏰ Напоминание: купить хлеб", 42)]


@pytest.mark.asyncio
async def test_a_reminder_is_never_enriched_by_another_spaces_contact(
    engine: AsyncEngine,
) -> None:
    # Контакт «Ави» живёт в пространстве B — напоминание пространства A с тем же
    # именем уходит БЕЗ номера (изоляция пространств и на доставке).
    await seed_contact(engine, ACCESS_B, name="Ави", phone="+972-50-111-22-33")
    await seed_reminder(engine, ACCESS_A, title="позвонить Ави", update_id=650)
    spy = SpyReminderDelivery()

    await delivery_step(engine, spy).process_once(ACCESS_A, NOW)

    assert spy.sent == [("⏰ Напоминание: позвонить Ави", 42)]

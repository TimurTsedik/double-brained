"""Журнал захвата умеет честно записать НЕ-телеграмный захват (эпик API-1, D1).

Проверяется то, ради чего строка расширена именно так: три телеграмных
идентификатора перестали быть обязательными, но строка от этого не стала
бесформенной — предикат формы ``ck_capture_events_channel`` держит её
fail-closed по каждому каналу отдельно. Отдельно проверяется, что происхождение
доезжает НЕ ТОЛЬКО до строки, но и до возвращаемой сущности: пока значение было
зашито в ``_to_entity``, строка была правильной, а ответ — врал.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventWriter,
)
from second_brain.slices.capture.application.contracts import (
    CaptureImageCommand,
    CaptureTextCommand,
    CaptureVoiceCommand,
    TelegramPhotoMetadata,
    TelegramVoiceMetadata,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
TRACE_ID = "b" * 32
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_capture_schema(
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
                    "id": ACCESS.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": ACCESS.user_space_id,
                    "owner_user_id": ACCESS.user_id,
                    "timezone": "Asia/Jerusalem",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ],
        )


def row_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": uuid4(),
        "user_space_id": ACCESS.user_space_id,
        "source_kind": "text",
        "channel": "telegram",
        "bot_id": 100,
        "telegram_update_id": 500,
        "telegram_message_id": 1500,
        "raw_text": "купить лампочку",
        "received_at": NOW,
        "created_at": NOW,
        "trace_id": TRACE_ID,
    }
    values.update(overrides)
    return values


async def insert_row(schema_engine: AsyncEngine, **overrides: object) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(insert(CaptureEventModel), [row_values(**overrides)])


def api_command(**overrides: object) -> CaptureTextCommand:
    arguments: dict[str, object] = {
        "access_context": ACCESS,
        "channel": "api",
        "client_ref": uuid4().hex,
        "request_tz": "Europe/Lisbon",
        "raw_text": "купить лампочку",
        "received_at": NOW,
        "trace_id": TRACE_ID,
    }
    arguments.update(overrides)
    return CaptureTextCommand(**arguments)  # type: ignore[arg-type]


def telegram_command(**overrides: object) -> CaptureTextCommand:
    arguments: dict[str, object] = {
        "access_context": ACCESS,
        "bot_id": 100,
        "telegram_update_id": 700,
        "telegram_message_id": 1700,
        "raw_text": "купить лампочку",
        "received_at": NOW,
        "trace_id": TRACE_ID,
    }
    arguments.update(overrides)
    return CaptureTextCommand(**arguments)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# форма строки: гибрид не бывает ни в одну сторону
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_telegram_row_without_telegram_ids_is_refused(
    schema_engine: AsyncEngine,
) -> None:
    # То самое fail-closed, которое отпущенные NOT NULL иначе бы отдали даром:
    # забытый channel= у нового вызывающего именно так и выглядит.
    with pytest.raises(IntegrityError):
        await insert_row(schema_engine, bot_id=None)


@pytest.mark.asyncio
async def test_an_api_row_carrying_telegram_ids_is_refused(
    schema_engine: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        await insert_row(
            schema_engine,
            channel="api",
            client_ref="ref-1",
            request_tz="Europe/Lisbon",
            telegram_update_id=None,
            telegram_message_id=None,
        )


@pytest.mark.asyncio
async def test_an_api_row_without_a_client_ref_is_refused(
    schema_engine: AsyncEngine,
) -> None:
    for client_ref in (None, ""):
        with pytest.raises(IntegrityError):
            await insert_row(
                schema_engine,
                channel="api",
                client_ref=client_ref,
                request_tz="Europe/Lisbon",
                bot_id=None,
                telegram_update_id=None,
                telegram_message_id=None,
            )


@pytest.mark.asyncio
async def test_two_api_captures_without_telegram_ids_coexist(
    schema_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    # uq_capture_events_telegram_delivery оставлен нетронутым: в PostgreSQL
    # UNIQUE по умолчанию NULLS DISTINCT, поэтому (NULL, NULL) уживаются сколько
    # угодно раз. Пусть это утверждает база, а не рассуждение.
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        writer = PostgresCaptureEventWriter(session)
        await writer.create(api_command(client_ref="ref-1"))
        await writer.create(api_command(client_ref="ref-2"))

    async with schema_engine.begin() as connection:
        stored = list(
            await connection.scalars(
                select(CaptureEventModel.client_ref).where(
                    CaptureEventModel.channel == "api"
                )
            )
        )
    assert sorted(stored) == ["ref-1", "ref-2"]


# ---------------------------------------------------------------------------
# происхождение доезжает и до строки, и до сущности
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_reaches_both_the_row_and_the_entity(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        event = await PostgresCaptureEventWriter(session).create(
            api_command(client_ref="ref-channel")
        )

    async with schema_engine.begin() as connection:
        channel = await connection.scalar(
            select(CaptureEventModel.channel).where(CaptureEventModel.id == event.id)
        )
    assert channel == "api"
    # Вторая половина — та, ради которой тест и заведён: значение бралось из
    # литерала в ``_to_entity``, поэтому строка была верной, а ответ врал.
    assert event.channel == "api"
    assert event.request_tz == "Europe/Lisbon"
    assert event.bot_id is None


# ---------------------------------------------------------------------------
# modality: расшифровка речи — это текст с пометкой происхождения
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_transcript_is_a_text_capture_with_provenance(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        event = await PostgresCaptureEventWriter(session).create(
            api_command(client_ref="ref-voice", modality="voice_transcript")
        )

    async with schema_engine.begin() as connection:
        row = (
            await connection.execute(
                select(
                    CaptureEventModel.source_kind,
                    CaptureEventModel.raw_text,
                    CaptureEventModel.modality,
                ).where(CaptureEventModel.id == event.id)
            )
        ).one()
        attachments = await connection.scalar(
            select(TelegramAttachmentModel.id).where(
                TelegramAttachmentModel.capture_event_id == event.id
            )
        )
    assert row.source_kind.value == "text"
    assert row.raw_text == "купить лампочку"
    assert row.modality == "voice_transcript"
    assert attachments is None


@pytest.mark.asyncio
async def test_telegram_capture_gets_the_default_modality(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Ловит пропущенный server_default на модели: create_voice/create_image
    # ``modality`` не передают вовсе, и на свежей базе первый же голос или фото
    # упал бы на NOT NULL.
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        writer = PostgresCaptureEventWriter(session)
        text_event = await writer.create(telegram_command())
        voice_event = await writer.create_voice(
            CaptureVoiceCommand(
                access_context=ACCESS,
                bot_id=100,
                telegram_update_id=701,
                telegram_message_id=1701,
                voice=TelegramVoiceMetadata(
                    file_id="voice-file",
                    file_unique_id="voice-unique",
                    duration_seconds=3,
                    file_size=None,
                    mime_type=None,
                ),
                received_at=NOW,
                trace_id=TRACE_ID,
            )
        )
        image_event = await writer.create_image(
            CaptureImageCommand(
                access_context=ACCESS,
                bot_id=100,
                telegram_update_id=702,
                telegram_message_id=1702,
                photo=TelegramPhotoMetadata(
                    file_id="photo-file",
                    file_unique_id="photo-unique",
                    width=10,
                    height=20,
                    file_size=None,
                ),
                caption=None,
                received_at=NOW,
                trace_id=TRACE_ID,
            )
        )

    async with schema_engine.begin() as connection:
        modalities = list(
            await connection.scalars(
                select(CaptureEventModel.modality)
                .where(
                    CaptureEventModel.id.in_(
                        [text_event.id, voice_event.id, image_event.id]
                    )
                )
                .order_by(CaptureEventModel.telegram_update_id)
            )
        )
    assert modalities == ["text", "text", "text"]

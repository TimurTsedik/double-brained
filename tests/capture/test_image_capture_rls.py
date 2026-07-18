"""Изоляция и инварианты image-attachment'ов (S2).

RLS прячет чужие фото-строки (file_id — PII), составной FK держит связку
kind=source_kind в одном пространстве, kind-условный CHECK не даёт фото без
размеров и голосу — с размерами; storage-метаданные оригинала неизменяемы.
"""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresImageSourceRepository,
)
from second_brain.slices.capture.application.contracts import MarkImageStoredCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 18, 11, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_image_capture_schema(
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
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for access in (ACCESS_A, ACCESS_B)
            ],
        )


def _capture_values(access: AccessContext, update_id: int) -> dict[str, object]:
    return {
        "id": uuid4(),
        "user_space_id": access.user_space_id,
        "source_kind": "image",
        "channel": "telegram",
        "bot_id": 1,
        "telegram_update_id": update_id,
        "telegram_message_id": update_id + 1_000,
        "raw_text": None,
        "received_at": NOW,
        "created_at": NOW,
        "trace_id": f"{update_id:x}".rjust(32, "1")[-32:],
    }


def _attachment_values(
    access: AccessContext,
    capture_event_id: UUID,
    *,
    file_id: str,
    width: int | None = 1280,
    height: int | None = 853,
    duration_seconds: int | None = None,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "user_space_id": access.user_space_id,
        "capture_event_id": capture_event_id,
        "kind": "image",
        "telegram_file_id": file_id,
        "telegram_file_unique_id": f"unique-{file_id}",
        "duration_seconds": duration_seconds,
        "width": width,
        "height": height,
        "telegram_file_size": 222_333,
        "telegram_mime_type": None,
        "storage_key": None,
        "sha256": None,
        "stored_size": None,
        "stored_mime_type": None,
        "stored_at": None,
        "created_at": NOW,
        "trace_id": "a" * 32,
    }


async def _seed_image(
    schema_engine: AsyncEngine, access: AccessContext, update_id: int, file_id: str
) -> UUID:
    capture_values = _capture_values(access, update_id)
    async with schema_engine.begin() as connection:
        await connection.execute(insert(CaptureEventModel).values(**capture_values))
        await connection.execute(
            insert(TelegramAttachmentModel).values(
                **_attachment_values(
                    access, cast(UUID, capture_values["id"]), file_id=file_id
                )
            )
        )
    return cast(UUID, capture_values["id"])


@pytest.mark.asyncio
async def test_image_attachment_requires_dimensions_and_no_duration(
    schema_engine: AsyncEngine,
) -> None:
    capture_values = _capture_values(ACCESS_A, 200)
    async with schema_engine.begin() as connection:
        await connection.execute(insert(CaptureEventModel).values(**capture_values))

    for broken in (
        # Фото без размеров — нарушение kind-условного CHECK'а.
        {"width": None, "height": None},
        # Фото с «длительностью» — тоже.
        {"duration_seconds": 5},
    ):
        async with schema_engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    insert(TelegramAttachmentModel).values(
                        **_attachment_values(
                            ACCESS_A,
                            cast(UUID, capture_values["id"]),
                            file_id="broken-image",
                            **broken,  # type: ignore[arg-type]
                        )
                    )
                )
            await transaction.rollback()


@pytest.mark.asyncio
async def test_image_attachment_parent_cannot_cross_user_spaces(
    schema_engine: AsyncEngine,
) -> None:
    capture_values = _capture_values(ACCESS_B, 201)
    async with schema_engine.begin() as connection:
        await connection.execute(insert(CaptureEventModel).values(**capture_values))

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(TelegramAttachmentModel).values(
                    **_attachment_values(
                        ACCESS_A,
                        cast(UUID, capture_values["id"]),
                        file_id="B-private-photo-id",
                    )
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_image_rls_hides_other_space_rows_and_file_ids(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_b = await _seed_image(schema_engine, ACCESS_B, 202, "B-private-photo-id")

    # Пространство A под RLS не видит фото-строку B ни чтением, ни портом.
    async with create_session_factory(engine)() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('second_brain.user_space_id', "
                    ":user_space_id, true)"
                ),
                {"user_space_id": str(ACCESS_A.user_space_id)},
            )
            visible = await session.scalar(
                select(func.count()).select_from(TelegramAttachmentModel)
            )
    assert visible == 0
    with pytest.raises(LookupError):
        await PostgresImageSourceRepository(
            create_session_factory(engine)
        ).get_image_source(ACCESS_A, capture_b)


@pytest.mark.asyncio
async def test_image_storage_metadata_is_write_once(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    from second_brain.slices.capture.adapters.persistence.repository import (
        PostgresImageAttachmentWriter,
    )

    capture_a = await _seed_image(schema_engine, ACCESS_A, 203, "A-private-photo-id")
    session_factory = create_session_factory(engine)

    def command(sha: str, stored_at: datetime) -> MarkImageStoredCommand:
        return MarkImageStoredCommand(
            access_context=ACCESS_A,
            capture_event_id=capture_a,
            storage_key=f"{ACCESS_A.user_space_id}/{capture_a}/original.jpg",
            sha256=sha,
            stored_size=17,
            stored_mime_type="image/jpeg",
            stored_at=stored_at,
        )

    first = command("c" * 64, NOW + timedelta(seconds=1))
    async with session_factory() as session:
        async with session.begin():
            await PostgresImageAttachmentWriter(session).mark_stored(first)
    # Повтор той же выгрузки — идемпотентный no-op…
    async with session_factory() as session:
        async with session.begin():
            await PostgresImageAttachmentWriter(session).mark_stored(first)
    # …в том числе с ДРУГИМ completed_at (истёкший lease → повторное
    # завершение): идентичность выгрузки — по байтам, не по времени.
    async with session_factory() as session:
        async with session.begin():
            await PostgresImageAttachmentWriter(session).mark_stored(
                command("c" * 64, NOW + timedelta(minutes=30))
            )
    # Первый stored_at сохранён (повтор его не двигает).
    async with create_session_factory(schema_engine)() as session:
        row = await session.scalar(
            select(TelegramAttachmentModel).where(
                TelegramAttachmentModel.capture_event_id == capture_a
            )
        )
    assert row is not None
    assert row.stored_at == NOW + timedelta(seconds=1)
    # …а перезапись другими байтами — запрещена (оригинал неизменяем).
    with pytest.raises(ValueError, match="immutable"):
        async with session_factory() as session:
            async with session.begin():
                await PostgresImageAttachmentWriter(session).mark_stored(
                    command("d" * 64, NOW + timedelta(seconds=2))
                )

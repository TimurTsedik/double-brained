from datetime import UTC, datetime
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
from second_brain.slices.capture.domain.entities import CaptureSourceKind
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 11, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_voice_capture_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [_user(ACCESS_A), _user(ACCESS_B)])
        await connection.execute(
            insert(UserSpace), [_space(ACCESS_A), _space(ACCESS_B)]
        )


def _user(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_id,
        "role": "admin",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _space(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_space_id,
        "owner_user_id": access.user_id,
        "timezone": "Asia/Jerusalem",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _capture_values(
    access: AccessContext,
    *,
    source_kind: str,
    raw_text: str | None,
    update_id: int,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "user_space_id": access.user_space_id,
        "source_kind": source_kind,
        "channel": "telegram",
        "bot_id": 1,
        "telegram_update_id": update_id,
        "telegram_message_id": update_id + 1_000,
        "raw_text": raw_text,
        "received_at": NOW,
        "created_at": NOW,
        "trace_id": f"{update_id:x}".rjust(32, "1")[-32:],
    }


def _attachment_values(
    access: AccessContext, capture_event_id: UUID, *, file_id: str
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "user_space_id": access.user_space_id,
        "capture_event_id": capture_event_id,
        "kind": "voice",
        "telegram_file_id": file_id,
        "telegram_file_unique_id": f"unique-{file_id}",
        "duration_seconds": 12,
        "telegram_file_size": 1_234,
        "telegram_mime_type": "audio/ogg",
        "storage_key": None,
        "sha256": None,
        "stored_size": None,
        "stored_mime_type": None,
        "stored_at": None,
        "created_at": NOW,
        "trace_id": "a" * 32,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "raw_text"),
    [
        ("voice", "voice must not have raw text"),
        ("text", None),
        ("text", ""),
    ],
)
async def test_capture_event_rejects_invalid_kind_and_text_combinations(
    schema_engine: AsyncEngine, source_kind: str, raw_text: str | None
) -> None:
    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(CaptureEventModel).values(
                    **_capture_values(
                        ACCESS_A,
                        source_kind=source_kind,
                        raw_text=raw_text,
                        update_id=100,
                    )
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_voice_attachment_parent_cannot_cross_user_spaces(
    schema_engine: AsyncEngine,
) -> None:
    capture_values = _capture_values(
        ACCESS_B, source_kind="voice", raw_text=None, update_id=101
    )
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
                        file_id="B-private-file-id",
                    )
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_voice_attachment_rejects_a_text_capture_parent(
    schema_engine: AsyncEngine,
) -> None:
    capture_values = _capture_values(
        ACCESS_A, source_kind="text", raw_text="text source", update_id=103
    )
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
                        file_id="not-a-voice-file",
                    )
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_attachment_rls_hides_other_space_and_file_ids(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_values = _capture_values(
        ACCESS_B, source_kind="voice", raw_text=None, update_id=102
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(CaptureEventModel).values(**capture_values))
        await connection.execute(
            insert(TelegramAttachmentModel).values(
                **_attachment_values(
                    ACCESS_B,
                    cast(UUID, capture_values["id"]),
                    file_id="B-private-file-id",
                )
            )
        )

    async with create_session_factory(engine)() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('second_brain.user_space_id', "
                    ":user_space_id, true)"
                ),
                {"user_space_id": str(ACCESS_A.user_space_id)},
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(TelegramAttachmentModel)
                )
                == 0
            )


@pytest.mark.asyncio
async def test_attachment_has_forced_rls_and_only_attachment_is_mutable(
    engine: AsyncEngine, isolated_database: IsolatedDatabase
) -> None:
    async with create_session_factory(engine)() as session:
        qualified_attachment = f'"{isolated_database.schema}"."telegram_attachments"'
        flags = (
            await session.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c WHERE c.oid = to_regclass(:table_name)"
                ),
                {"table_name": qualified_attachment},
            )
        ).one()
        assert flags == (True, True)
        assert (
            await session.scalar(
                text(
                    "SELECT has_column_privilege(current_user, :table_name, "
                    "'storage_key', 'UPDATE')"
                ),
                {"table_name": qualified_attachment},
            )
            is True
        )
        assert (
            await session.scalar(
                text(
                    "SELECT has_column_privilege(current_user, :table_name, "
                    "'telegram_file_id', 'UPDATE')"
                ),
                {"table_name": qualified_attachment},
            )
            is False
        )
        assert (
            await session.scalar(
                text("SELECT has_table_privilege(current_user, :table_name, 'UPDATE')"),
                {"table_name": (f'"{isolated_database.schema}"."capture_events"')},
            )
            is False
        )


def test_attachment_representation_hides_telegram_identifiers() -> None:
    model = TelegramAttachmentModel(
        **_attachment_values(
            ACCESS_A,
            UUID("00000000-0000-0000-0000-000000000101"),
            file_id="repr-private-file-id",
        )
    )

    assert "repr-private-file-id" not in repr(model)
    assert "unique-repr-private-file-id" not in repr(model)
    assert CaptureSourceKind.VOICE.value == "voice"

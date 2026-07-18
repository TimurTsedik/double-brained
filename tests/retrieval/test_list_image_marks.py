"""Флаг изображения-источника в СПИСКАХ (спека §2.2) — живой PostgreSQL.

Точный поиск и страница сводки несут has_image_source коррелированным EXISTS
в ТОМ ЖЕ union-запросе (не по запросу на строку результата); reader показа
отдаёт (file_id, storage_key) image-attachment'а для отправки самого фото.
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import (
    TelegramAttachmentModel,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresDigestReader,
    PostgresExactSearchWriter,
    PostgresRecordViewReader,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.test_record_view import add_image_capture, add_note
from tests.retrieval.test_semantic_index_persistence import (
    ACCESS_A,
    NOW,
    TRACE_ID,
    add_capture,
    space_row,
    user_row,
)


@pytest_asyncio.fixture(autouse=True)
async def reset_list_marks_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [user_row(ACCESS_A)])
        await connection.execute(insert(UserSpace), [space_row(ACCESS_A)])


async def _add_attachment(
    schema_engine: AsyncEngine,
    access: AccessContext,
    capture_event_id: UUID,
    *,
    storage_key: str | None,
) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(TelegramAttachmentModel).values(
                id=uuid4(),
                user_space_id=access.user_space_id,
                capture_event_id=capture_event_id,
                kind="image",
                telegram_file_id="private-photo-id",
                telegram_file_unique_id="private-photo-unique",
                duration_seconds=None,
                width=1280,
                height=853,
                telegram_file_size=222_333,
                telegram_mime_type=None,
                storage_key=storage_key,
                sha256="e" * 64 if storage_key is not None else None,
                stored_size=17 if storage_key is not None else None,
                stored_mime_type="image/jpeg" if storage_key is not None else None,
                stored_at=NOW + timedelta(seconds=1)
                if storage_key is not None
                else None,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )


@pytest.mark.asyncio
async def test_exact_search_flags_image_sourced_records_in_one_query(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    text_capture = await add_capture(schema_engine, ACCESS_A)
    image_capture = await add_image_capture(schema_engine, ACCESS_A, "тайфун на море")
    plain_note = await add_note(schema_engine, ACCESS_A, text_capture, "тайфун в поле")
    photo_note = await add_note(
        schema_engine, ACCESS_A, image_capture, "тайфун на море"
    )

    async with create_session_factory(engine)() as session:
        async with session.begin():
            records = await PostgresExactSearchWriter(session).search(
                ACCESS_A, "тайфун", limit=10
            )

    flags = {record.id: record.has_image_source for record in records}
    assert flags == {plain_note: False, photo_note: True}


@pytest.mark.asyncio
async def test_digest_page_flags_image_sourced_records(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    text_capture = await add_capture(schema_engine, ACCESS_A)
    image_capture = await add_image_capture(schema_engine, ACCESS_A, "подпись")
    plain_note = await add_note(schema_engine, ACCESS_A, text_capture, "обычная")
    photo_note = await add_note(schema_engine, ACCESS_A, image_capture, "подпись")

    async with create_session_factory(engine)() as session:
        async with session.begin():
            page = await PostgresDigestReader(session).read_page(
                ACCESS_A,
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                offset=0,
                limit=10,
            )

    flags = {item.id: item.has_image_source for item in page}
    assert flags == {plain_note: False, photo_note: True}


@pytest.mark.asyncio
async def test_image_attachment_reader_returns_file_id_and_storage_key(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    image_capture = await add_image_capture(schema_engine, ACCESS_A, "подпись")
    stored_capture = await add_image_capture(schema_engine, ACCESS_A, "скачанная")
    text_capture = await add_capture(schema_engine, ACCESS_A)
    pending_note = await add_note(schema_engine, ACCESS_A, image_capture, "подпись")
    stored_note = await add_note(schema_engine, ACCESS_A, stored_capture, "скачанная")
    text_note = await add_note(schema_engine, ACCESS_A, text_capture, "текстовая")
    await _add_attachment(schema_engine, ACCESS_A, image_capture, storage_key=None)
    stored_key = f"{ACCESS_A.user_space_id}/{stored_capture}/original.jpg"
    await _add_attachment(
        schema_engine, ACCESS_A, stored_capture, storage_key=stored_key
    )

    async with create_session_factory(engine)() as session:
        async with session.begin():
            reader = PostgresRecordViewReader(session)
            pending = await reader.image_attachment(
                ACCESS_A, SearchRecordType.NOTE, pending_note
            )
            stored = await reader.image_attachment(
                ACCESS_A, SearchRecordType.NOTE, stored_note
            )
            missing = await reader.image_attachment(
                ACCESS_A, SearchRecordType.NOTE, text_note
            )

    # Байты ещё не скачаны → storage_key None (fast path остаётся единственным).
    assert pending == ("private-photo-id", None)
    assert stored == ("private-photo-id", stored_key)
    assert missing is None

"""Реконсиляция живой БД до S2 «Изображения».

Живая прод-база (текст/голос) несёт старые CHECK'и (source_kind без 'image',
kind='voice', step_type без 'image_download'), NOT NULL на duration_seconds и
output_type и не имеет колонок width/height/source_only.
create_all(checkfirst=True) существующие таблицы не трогает — доращивать
обязан initialize_schema, идемпотентно.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import initialize_schema
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
TRACE_ID = "9" * 32


async def _downgrade_to_pre_image_schema(
    schema_engine: AsyncEngine, schema: str
) -> None:
    events = f'"{schema}".capture_events'
    attachments = f'"{schema}".telegram_attachments'
    steps = f'"{schema}".processing_steps'
    runs = f'"{schema}".processing_runs'
    async with schema_engine.begin() as connection:
        for statement in (
            # capture_events: старый набор source_kind и старый kind_content.
            f'ALTER TABLE {events} DROP CONSTRAINT "capture_source_kind"',
            f'ALTER TABLE {events} ADD CONSTRAINT "capture_source_kind" '
            "CHECK (source_kind IN ('text', 'voice'))",
            f"ALTER TABLE {events} DROP CONSTRAINT ck_capture_events_kind_content",
            f"ALTER TABLE {events} ADD CONSTRAINT ck_capture_events_kind_content "
            "CHECK ((source_kind = 'text' AND raw_text IS NOT NULL "
            "AND raw_text <> '') OR (source_kind = 'voice' AND raw_text IS NULL))",
            # attachments: voice-only мир без фото-колонок.
            f"ALTER TABLE {attachments} DROP CONSTRAINT "
            "ck_telegram_attachments_kind_fields",
            f"ALTER TABLE {attachments} DROP CONSTRAINT "
            "ck_telegram_attachments_dimensions",
            f"ALTER TABLE {attachments} DROP COLUMN width",
            f"ALTER TABLE {attachments} DROP COLUMN height",
            f"ALTER TABLE {attachments} ALTER COLUMN duration_seconds SET NOT NULL",
            f'ALTER TABLE {attachments} DROP CONSTRAINT "telegram_attachment_kind"',
            f'ALTER TABLE {attachments} ADD CONSTRAINT "telegram_attachment_kind" '
            "CHECK (kind IN ('text', 'voice'))",
            f"ALTER TABLE {attachments} DROP CONSTRAINT ck_telegram_attachments_kind",
            f"ALTER TABLE {attachments} ADD CONSTRAINT "
            "ck_telegram_attachments_kind CHECK (kind = 'voice')",
            # processing: шаги без image_download, output_type NOT NULL.
            f'ALTER TABLE {steps} DROP CONSTRAINT "processing_step_type"',
            f'ALTER TABLE {steps} ADD CONSTRAINT "processing_step_type" CHECK '
            "(step_type IN ('audio_download', 'transcription', 'classification', "
            "'indexing'))",
            f"ALTER TABLE {runs} DROP CONSTRAINT "
            "ck_processing_runs_output_type_source_only",
            f"ALTER TABLE {runs} DROP COLUMN source_only",
            f"ALTER TABLE {runs} ALTER COLUMN output_type SET NOT NULL",
        ):
            await connection.execute(text(statement))


@pytest.mark.asyncio
async def test_initialize_grows_live_db_to_accept_image_rows(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    await _downgrade_to_pre_image_schema(schema_engine, schema)

    # Повторная инициализация на «живой» схеме обязана дорастить её до S2.
    await initialize_schema(schema_engine, schema)

    user_id, space_id = uuid4(), uuid4()
    capture_id, run_id = uuid4(), uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".users '
                "(id, role, is_active, created_at, updated_at) "
                "VALUES (:id, 'member', true, :ts, :ts)"
            ),
            {"id": user_id, "ts": NOW},
        )
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".user_spaces '
                "(id, owner_user_id, timezone, is_active, created_at, updated_at) "
                "VALUES (:id, :owner, 'Asia/Jerusalem', true, :ts, :ts)"
            ),
            {"id": space_id, "owner": user_id, "ts": NOW},
        )
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".capture_events '
                "(id, user_space_id, source_kind, channel, bot_id, "
                "telegram_update_id, telegram_message_id, raw_text, received_at, "
                "created_at, trace_id) VALUES "
                "(:id, :space, 'image', 'telegram', 1, 2, 3, NULL, :ts, :ts, :trace)"
            ),
            {"id": capture_id, "space": space_id, "ts": NOW, "trace": TRACE_ID},
        )
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".telegram_attachments '
                "(id, user_space_id, capture_event_id, kind, telegram_file_id, "
                "telegram_file_unique_id, duration_seconds, width, height, "
                "telegram_file_size, telegram_mime_type, created_at, trace_id) "
                "VALUES (:id, :space, :capture, 'image', 'file', 'unique', "
                "NULL, 1280, 853, 100, NULL, :ts, :trace)"
            ),
            {
                "id": uuid4(),
                "space": space_id,
                "capture": capture_id,
                "ts": NOW,
                "trace": TRACE_ID,
            },
        )
        # Source-only прогон: output_type NULL легален ТОЛЬКО с source_only.
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".processing_runs '
                "(id, user_space_id, capture_event_id, output_type, source_only, "
                "route_default_by_time, version, created_at, updated_at, trace_id) "
                "VALUES (:id, :space, :capture, NULL, true, false, 1, :ts, :ts, "
                ":trace)"
            ),
            {
                "id": run_id,
                "space": space_id,
                "capture": capture_id,
                "ts": NOW,
                "trace": TRACE_ID,
            },
        )
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".processing_steps '
                "(id, user_space_id, processing_run_id, step_type, status, "
                "attempt_count, next_attempt_at, created_at, updated_at, trace_id) "
                "VALUES (:id, :space, :run, 'image_download', 3, 0, :ts, :ts, :ts, "
                ":trace)"
            ),
            {
                "id": uuid4(),
                "space": space_id,
                "run": run_id,
                "ts": NOW,
                "trace": TRACE_ID,
            },
        )
        stored = await connection.scalar(
            text(f'SELECT source_kind FROM "{schema}".capture_events WHERE id = :id'),
            {"id": capture_id},
        )
    assert stored == "image"


@pytest.mark.asyncio
async def test_reconciled_db_still_rejects_untyped_non_source_only_run(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    schema = isolated_database.schema
    await initialize_schema(schema_engine, schema)

    user_id, space_id, capture_id = uuid4(), uuid4(), uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".users '
                "(id, role, is_active, created_at, updated_at) "
                "VALUES (:id, 'member', true, :ts, :ts)"
            ),
            {"id": user_id, "ts": NOW},
        )
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".user_spaces '
                "(id, owner_user_id, timezone, is_active, created_at, updated_at) "
                "VALUES (:id, :owner, 'Asia/Jerusalem', true, :ts, :ts)"
            ),
            {"id": space_id, "owner": user_id, "ts": NOW},
        )
        await connection.execute(
            text(
                f'INSERT INTO "{schema}".capture_events '
                "(id, user_space_id, source_kind, channel, bot_id, "
                "telegram_update_id, telegram_message_id, raw_text, received_at, "
                "created_at, trace_id) VALUES "
                "(:id, :space, 'image', 'telegram', 1, 4, 5, NULL, :ts, :ts, :trace)"
            ),
            {"id": capture_id, "space": space_id, "ts": NOW, "trace": TRACE_ID},
        )

    # NULL-тип БЕЗ source_only — порча данных, база обязана отбить.
    with pytest.raises(Exception, match="ck_processing_runs_output_type_source_only"):
        async with schema_engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema}".processing_runs '
                    "(id, user_space_id, capture_event_id, output_type, "
                    "source_only, route_default_by_time, version, created_at, "
                    "updated_at, trace_id) VALUES (:id, :space, :capture, NULL, "
                    "false, false, 1, :ts, :ts, :trace)"
                ),
                {
                    "id": uuid4(),
                    "space": space_id,
                    "capture": capture_id,
                    "ts": NOW,
                    "trace": TRACE_ID,
                },
            )

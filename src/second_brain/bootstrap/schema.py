from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.schema import (
    initialize_identity_schema,
    reset_identity_prototype_schema,
)

CAPTURE_EVENT_TABLE = cast(Table, CaptureEventModel.__table__)


async def initialize_schema(engine: AsyncEngine, schema_name: str = "public") -> None:
    await initialize_identity_schema(engine, schema_name)
    await _initialize_capture_schema(engine, schema_name)


async def reset_prototype_schema(
    engine: AsyncEngine, confirm: bool, schema_name: str = "public"
) -> None:
    if not confirm:
        await reset_identity_prototype_schema(engine, confirm, schema_name)
        return
    await _drop_capture_schema(engine)
    await reset_identity_prototype_schema(engine, confirm, schema_name)
    await _initialize_capture_schema(engine, schema_name)


async def _initialize_capture_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_capture_event_table)
        await _configure_capture_event_rls(connection, schema_name)


async def _drop_capture_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_capture_event_table)


def _create_capture_event_table(connection: Connection) -> None:
    CAPTURE_EVENT_TABLE.create(connection, checkfirst=True)


def _drop_capture_event_table(connection: Connection) -> None:
    CAPTURE_EVENT_TABLE.drop(connection, checkfirst=True)


async def _configure_capture_event_rls(
    connection: AsyncConnection, schema_name: str
) -> None:
    capture_events = f"{_quote_identifier(schema_name)}.capture_events"
    await connection.execute(
        text(f"ALTER TABLE {capture_events} ENABLE ROW LEVEL SECURITY")
    )
    await connection.execute(
        text(f"ALTER TABLE {capture_events} FORCE ROW LEVEL SECURITY")
    )
    await connection.execute(
        text(
            f"DROP POLICY IF EXISTS capture_events_user_space_scope ON {capture_events}"
        )
    )
    await connection.execute(
        text(
            "CREATE POLICY capture_events_user_space_scope ON "
            f"{capture_events} "
            "USING (user_space_id = NULLIF("
            "current_setting('second_brain.user_space_id', true), ''"
            ")::uuid) "
            "WITH CHECK (user_space_id = NULLIF("
            "current_setting('second_brain.user_space_id', true), ''"
            ")::uuid)"
        )
    )


def _quote_identifier(value: str) -> str:
    return f'"{value.replace('"', '""')}"'

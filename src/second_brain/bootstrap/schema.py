from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.schema import (
    APPLICATION_ROLE,
    initialize_identity_schema,
    reset_identity_prototype_schema,
)
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingTaskModeModel,
    TaskModel,
    TaskProvenanceModel,
)

CAPTURE_EVENT_TABLE = cast(Table, CaptureEventModel.__table__)
TASK_TABLES = (
    cast(Table, TaskModel.__table__),
    cast(Table, TaskProvenanceModel.__table__),
    cast(Table, PendingTaskModeModel.__table__),
)


async def initialize_schema(engine: AsyncEngine, schema_name: str = "public") -> None:
    await initialize_identity_schema(engine, schema_name)
    await _initialize_capture_schema(engine, schema_name)
    await _initialize_task_schema(engine, schema_name)


async def reset_prototype_schema(
    engine: AsyncEngine, confirm: bool, schema_name: str = "public"
) -> None:
    if not confirm:
        await reset_identity_prototype_schema(engine, confirm, schema_name)
        return
    await _drop_task_schema(engine)
    await _drop_capture_schema(engine)
    await reset_identity_prototype_schema(engine, confirm, schema_name)
    await _initialize_capture_schema(engine, schema_name)
    await _initialize_task_schema(engine, schema_name)


async def _initialize_capture_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_capture_event_table)
        await _configure_capture_event_rls(connection, schema_name)


async def _drop_capture_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_capture_event_table)


async def _initialize_task_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_task_tables)
        for table_name in ("tasks", "task_provenance", "pending_task_modes"):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_task_privileges(connection, schema_name)


async def _drop_task_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_task_tables)


def _create_capture_event_table(connection: Connection) -> None:
    CAPTURE_EVENT_TABLE.create(connection, checkfirst=True)


def _drop_capture_event_table(connection: Connection) -> None:
    CAPTURE_EVENT_TABLE.drop(connection, checkfirst=True)


def _create_task_tables(connection: Connection) -> None:
    for table in TASK_TABLES:
        table.create(connection, checkfirst=True)


def _drop_task_tables(connection: Connection) -> None:
    for table in reversed(TASK_TABLES):
        table.drop(connection, checkfirst=True)


async def _configure_capture_event_rls(
    connection: AsyncConnection, schema_name: str
) -> None:
    await _configure_user_space_rls(connection, schema_name, "capture_events")


async def _configure_user_space_rls(
    connection: AsyncConnection, schema_name: str, table_name: str
) -> None:
    table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
    policy_name = f"{table_name}_user_space_scope"
    await connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    await connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    await connection.execute(
        text(f"DROP POLICY IF EXISTS {_quote_identifier(policy_name)} ON {table}")
    )
    await connection.execute(
        text(
            f"CREATE POLICY {_quote_identifier(policy_name)} ON {table} "
            "USING (user_space_id = NULLIF("
            "current_setting('second_brain.user_space_id', true), ''"
            ")::uuid) "
            "WITH CHECK (user_space_id = NULLIF("
            "current_setting('second_brain.user_space_id', true), ''"
            ")::uuid)"
        )
    )


async def _grant_task_privileges(connection: AsyncConnection, schema_name: str) -> None:
    quoted_schema = _quote_identifier(schema_name)
    task_tables = (
        f"{quoted_schema}.tasks, {quoted_schema}.task_provenance, "
        f"{quoted_schema}.pending_task_modes"
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {task_tables} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {task_tables} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            "GRANT UPDATE ON TABLE "
            f"{quoted_schema}.pending_task_modes TO {APPLICATION_ROLE}"
        )
    )


def _quote_identifier(value: str) -> str:
    return f'"{value.replace('"', '""')}"'

from typing import cast

from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.persistence.base import Base
from second_brain.slices.identity.adapters.persistence.models import (
    RESULT_KIND_CHECK_NAME,
    EnrollmentAttempt,
    EnrollmentInvite,
    TelegramIdentity,
    TelegramUpdateReceipt,
    User,
    UserSpace,
)

APPLICATION_ROLE = "second_brain_app"
IDENTITY_TABLES = (
    cast(Table, User.__table__),
    cast(Table, UserSpace.__table__),
    cast(Table, TelegramIdentity.__table__),
    cast(Table, EnrollmentInvite.__table__),
    cast(Table, TelegramUpdateReceipt.__table__),
    cast(Table, EnrollmentAttempt.__table__),
)


async def initialize_identity_schema(
    engine: AsyncEngine, schema_name: str = "public"
) -> None:
    async with engine.begin() as connection:
        await _ensure_application_role(connection)
        await connection.run_sync(_create_identity_tables)
        await _reconcile_result_kind_check(connection, schema_name)
        await _grant_application_privileges(connection, schema_name)


async def reset_identity_prototype_schema(
    engine: AsyncEngine, confirm: bool, schema_name: str = "public"
) -> None:
    if not confirm:
        raise ValueError("prototype schema reset requires confirmation")

    async with engine.begin() as connection:
        await _ensure_application_role(connection)
        await connection.run_sync(_drop_identity_tables)
        await connection.run_sync(_create_identity_tables)
        await _grant_application_privileges(connection, schema_name)


async def _ensure_application_role(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            "DO $$ "
            "BEGIN "
            "CREATE ROLE second_brain_app LOGIN NOSUPERUSER NOBYPASSRLS; "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$"
        )
    )
    await connection.execute(
        text(
            "ALTER ROLE second_brain_app LOGIN NOSUPERUSER NOBYPASSRLS "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT"
        )
    )


def _create_identity_tables(connection: Connection) -> None:
    Base.metadata.create_all(connection, tables=IDENTITY_TABLES)


async def _reconcile_result_kind_check(
    connection: AsyncConnection, schema_name: str
) -> None:
    # create_all(checkfirst=True) skips an existing table, so a live prototype DB
    # (slices 1-2) keeps its OLD result_kind CHECK and would reject the memory_*
    # kinds. Re-apply the current ORM definition idempotently: harmless drop+add
    # of the same predicate on a fresh DB, a repair on an existing one. Existing
    # rows never violate the new set (it is a strict superset), so ADD is safe.
    expression = _result_kind_check_expression()
    table = f"{_quote_identifier(schema_name)}.telegram_update_receipts"
    quoted_name = _quote_identifier(RESULT_KIND_CHECK_NAME)
    await connection.execute(
        text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {quoted_name}")
    )
    await connection.execute(
        text(f"ALTER TABLE {table} ADD CONSTRAINT {quoted_name} CHECK ({expression})")
    )


def _result_kind_check_expression() -> str:
    table = cast(Table, TelegramUpdateReceipt.__table__)
    for constraint in table.constraints:
        if (
            isinstance(constraint, CheckConstraint)
            and constraint.name == RESULT_KIND_CHECK_NAME
        ):
            return str(constraint.sqltext)
    raise RuntimeError("result_kind CHECK constraint is missing from the ORM model")


def _drop_identity_tables(connection: Connection) -> None:
    Base.metadata.drop_all(connection, tables=IDENTITY_TABLES)


async def _grant_application_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    database_name = await connection.scalar(text("SELECT current_database()"))
    if not isinstance(database_name, str):
        raise RuntimeError("PostgreSQL did not return a database name")

    quoted_database = _quote_identifier(database_name)
    quoted_schema = _quote_identifier(schema_name)
    enrollment_attempts = f"{quoted_schema}.enrollment_attempts"
    enrollment_invites = f"{quoted_schema}.enrollment_invites"
    await connection.execute(
        text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
            f"{quoted_schema} FROM {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA "
            f"{quoted_schema} TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "GRANT UPDATE ON TABLE "
            f"{enrollment_attempts}, {enrollment_invites} "
            f"TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA "
            f"{quoted_schema} REVOKE ALL ON TABLES FROM {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA "
            f"{quoted_schema} GRANT SELECT, INSERT ON TABLES TO {APPLICATION_ROLE}"
        )
    )


def _quote_identifier(value: str) -> str:
    return f'"{value.replace('"', '""')}"'

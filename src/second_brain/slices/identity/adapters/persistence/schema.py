from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.persistence.base import Base
from second_brain.slices.identity.adapters.persistence.models import (
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

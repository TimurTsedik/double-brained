from datetime import UTC, datetime
from uuid import UUID

import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from tests.identity.conftest import (
    IsolatedDatabase,
    engine,
    isolated_database,
    schema_engine,
    session,
)

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000022"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_project_schema(
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
                    "role": "admin",
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


__all__ = ["engine", "isolated_database", "schema_engine", "session"]

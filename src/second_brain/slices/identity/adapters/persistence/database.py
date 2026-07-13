from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from second_brain.slices.identity.adapters.persistence.schema import APPLICATION_ROLE


def create_database_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def assert_non_privileged_application_role(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        )

    if result.one() != (APPLICATION_ROLE, False, False):
        raise RuntimeError(
            "DATABASE_URL must use the dedicated second_brain_app non-superuser "
            "PostgreSQL role without BYPASSRLS"
        )

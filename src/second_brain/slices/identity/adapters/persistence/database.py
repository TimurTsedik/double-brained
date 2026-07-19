from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from second_brain.slices.identity.adapters.persistence.schema import APPLICATION_ROLE
from second_brain.slices.identity.application.contracts import AccessContext


def create_database_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    """Объявляет базе пространство вызывающего на текущую транзакцию.

    Ровно тот же вызов, которым scope ставят репозитории слайсов: политики RLS
    сверяются с настройкой ``second_brain.user_space_id``, а третий аргумент
    ``true`` делает её транзакционной — за пределы транзакции она не утекает и
    следующий запрос на том же соединении её не унаследует.

    Своя копия этой строки живёт в репозиториях capture/tasks/classification и
    остаётся там намеренно: границы импортов запрещают persistence одного слайса
    зависеть от persistence другого. Здесь — общая точка для тех, кому она
    доступна: bootstrap и сам слайс identity.
    """
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


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

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresAccessContextResolver,
)
from second_brain.slices.identity.application.access_context import ResolveAccessContext
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def reset_access_context_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


async def create_identity(
    session: AsyncSession,
    telegram_user_id: int,
    *,
    user_active: bool = True,
    space_active: bool = True,
    identity_active: bool = True,
) -> tuple[User, UserSpace]:
    user = User(
        id=uuid4(),
        role="admin",
        is_active=user_active,
        created_at=NOW,
        updated_at=NOW,
    )
    space = UserSpace(
        id=uuid4(),
        owner_user_id=user.id,
        timezone="Asia/Jerusalem",
        is_active=space_active,
        created_at=NOW,
        updated_at=NOW,
    )
    identity = TelegramIdentity(
        id=uuid4(),
        telegram_user_id=telegram_user_id,
        user_id=user.id,
        is_active=identity_active,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(user)
    await session.flush()
    session.add_all([space, identity])
    await session.commit()
    return user, space


@pytest.mark.asyncio
async def test_server_side_resolver_maps_each_telegram_actor_only_to_own_active_space(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    first_user, first_space = await create_identity(session, 1001)
    second_user, second_space = await create_identity(session, 2002)
    resolver = ResolveAccessContext(
        PostgresAccessContextResolver(create_session_factory(engine))
    )

    first_context = await resolver.execute(1001)
    second_context = await resolver.execute(2002)

    assert first_context is not None
    assert second_context is not None
    assert (first_context.user_id, first_context.user_space_id) == (
        first_user.id,
        first_space.id,
    )
    assert (second_context.user_id, second_context.user_space_id) == (
        second_user.id,
        second_space.id,
    )
    assert first_context.user_space_id != second_context.user_space_id


@pytest.mark.asyncio
async def test_server_side_resolver_rejects_unmapped_and_inactive_identity_chains(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    await create_identity(session, 3003, identity_active=False)
    await create_identity(session, 4004, user_active=False)
    await create_identity(session, 5005, space_active=False)
    resolver = ResolveAccessContext(
        PostgresAccessContextResolver(create_session_factory(engine))
    )

    assert await resolver.execute(9999) is None
    assert await resolver.execute(3003) is None
    assert await resolver.execute(4004) is None
    assert await resolver.execute(5005) is None

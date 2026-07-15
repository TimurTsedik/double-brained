from importlib.util import find_spec

import pytest

from tests.identity.conftest import IsolatedDatabase


def test_healthcheck_module_exists() -> None:
    assert find_spec("second_brain.bootstrap.healthcheck") is not None


@pytest.mark.asyncio
async def test_ping_database_returns_true_for_live_database(
    isolated_database: IsolatedDatabase,
) -> None:
    from second_brain.bootstrap.healthcheck import ping_database

    assert await ping_database(isolated_database.database_url) is True


@pytest.mark.asyncio
async def test_ping_database_returns_false_for_unreachable_database() -> None:
    from second_brain.bootstrap.healthcheck import ping_database

    assert (
        await ping_database("postgresql+asyncpg://second_brain_app@127.0.0.1:1/absent")
        is False
    )

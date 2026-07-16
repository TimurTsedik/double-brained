from importlib.util import find_spec

import httpx
import pytest
from fastapi import FastAPI


async def get_health(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.get("/health")


# pytest-asyncio, не asyncio.run(): голый run() в sync-тесте оставляет плагину
# «чужой» текущий loop, и его финализатор заводит незакрытый loop — под
# -W error это роняло СЛЕДУЮЩИЙ тест (healthcheck) unraisable-ошибкой, как
# только перед этим модулем появился любой async-модуль (алфавитный порядок).
@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    assert find_spec("second_brain.bootstrap.app") is not None

    from second_brain.bootstrap.app import create_app

    response = await get_health(create_app())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_main_exposes_uvicorn_target() -> None:
    assert find_spec("second_brain.bootstrap.main") is not None

    from second_brain.bootstrap.main import app

    assert isinstance(app, FastAPI)

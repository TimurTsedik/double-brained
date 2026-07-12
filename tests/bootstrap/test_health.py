import asyncio
from importlib.util import find_spec

import httpx
from fastapi import FastAPI


async def get_health(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.get("/health")


def test_health_returns_ok() -> None:
    assert find_spec("second_brain.bootstrap.app") is not None

    from second_brain.bootstrap.app import create_app

    response = asyncio.run(get_health(create_app()))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_main_exposes_uvicorn_target() -> None:
    assert find_spec("second_brain.bootstrap.main") is not None

    from second_brain.bootstrap.main import app

    assert isinstance(app, FastAPI)

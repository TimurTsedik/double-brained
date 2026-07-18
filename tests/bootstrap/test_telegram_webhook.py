"""Webhook-роут B1: секрет в заголовке, cap тела, идемпотентный INSERT в INBOX.

Вся работа роута — один INSERT (обработки в HTTP-запросе НЕТ): 200 мгновенный,
ретраи Telegram гасятся конфликтом (bot_id, update_id). Секрета нет в env →
503 (webhook не сконфигурирован), неверный секрет → 401 без тела, кривой
payload → 4xx без 500, сбой БД → 500 (Telegram ретраит). Привилегированная
роль БД закрывает дверь, но НЕ маскируется под «не сконфигурирован».
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from second_brain.bootstrap.app import (
    SECRET_TOKEN_HEADER,
    TelegramWebhookRuntime,
    WebhookRuntimeProvider,
    _webhook_runtime_from_environment,
    create_app,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramUpdateInbox,
)
from second_brain.slices.identity.domain.entities import TelegramInboxStatus
from tests.identity.conftest import IsolatedDatabase

SECRET = "webhook-secret-value"
BOT_ID = 900
MAX_BODY_BYTES = 4096


@pytest_asyncio.fixture(autouse=True)
async def reset_webhook_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


def constant_provider(
    runtime: TelegramWebhookRuntime | None,
) -> WebhookRuntimeProvider:
    """Готовый runtime без похода в env (сборка роута асинхронная)."""

    async def provider() -> TelegramWebhookRuntime | None:
        return runtime

    return provider


def webhook_app(engine: AsyncEngine) -> FastAPI:
    runtime = TelegramWebhookRuntime(
        secret=SECRET,
        bot_id=BOT_ID,
        max_body_bytes=MAX_BODY_BYTES,
        session_factory=create_session_factory(engine),
    )
    return create_app(constant_provider(runtime))


async def post_webhook(
    app: FastAPI,
    content: bytes | AsyncIterator[bytes],
    secret: str | None = SECRET,
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers[SECRET_TOKEN_HEADER] = secret
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post("/telegram/webhook", content=content, headers=headers)


def update_body(update_id: int) -> bytes:
    return json.dumps(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id + 1000,
                "date": 1784000000,
                "chat": {"id": 42, "type": "private", "first_name": "Т"},
                "from": {"id": 42, "is_bot": False, "first_name": "Т"},
                "text": "webhook-секрет-текст",
            },
        }
    ).encode()


@pytest.mark.asyncio
async def test_unconfigured_webhook_responds_503_not_401(
    engine: AsyncEngine,
) -> None:
    app = create_app(constant_provider(None))

    response = await post_webhook(app, update_body(1), secret=None)

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_wrong_secret_responds_401_without_body(engine: AsyncEngine) -> None:
    app = webhook_app(engine)

    missing = await post_webhook(app, update_body(2), secret=None)
    wrong = await post_webhook(app, update_body(2), secret="not-the-secret")

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.content == b""
    assert wrong.content == b""


@pytest.mark.asyncio
async def test_valid_update_lands_in_inbox_as_pending(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    app = webhook_app(engine)

    response = await post_webhook(app, update_body(3))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    row = (await session.scalars(select(TelegramUpdateInbox))).one()
    assert (row.bot_id, row.update_id) == (BOT_ID, 3)
    assert row.status is TelegramInboxStatus.PENDING
    assert row.attempt_count == 0
    assert row.payload["message"]["text"] == "webhook-секрет-текст"


@pytest.mark.asyncio
async def test_telegram_retry_of_same_update_keeps_one_row(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    app = webhook_app(engine)

    first = await post_webhook(app, update_body(4))
    second = await post_webhook(app, update_body(4))

    assert first.status_code == 200
    assert second.status_code == 200
    rows = (await session.scalars(select(TelegramUpdateInbox))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_oversized_body_responds_413(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    app = webhook_app(engine)
    oversized = json.dumps(
        {"update_id": 5, "message": {"text": "x" * MAX_BODY_BYTES}}
    ).encode()

    response = await post_webhook(app, oversized)

    assert response.status_code == 413
    assert (await session.scalars(select(TelegramUpdateInbox))).all() == []


@pytest.mark.asyncio
async def test_chunked_oversized_body_is_cut_off_before_full_buffering(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    # Без Content-Length (chunked) ранний cap по заголовку не срабатывает:
    # тело обязано резаться ПО ХОДУ чтения, иначе лимит обходится по памяти.
    app = webhook_app(engine)
    chunk = b"x" * 1024
    total_chunks = 4 * (MAX_BODY_BYTES // len(chunk))
    delivered = 0

    async def chunked_body() -> AsyncIterator[bytes]:
        nonlocal delivered
        for _ in range(total_chunks):
            delivered += 1
            yield chunk

    response = await post_webhook(app, chunked_body())

    assert response.status_code == 413
    assert response.request.headers.get("transfer-encoding") == "chunked"
    # Чтение прекращено на первом же чанке за лимитом, остаток не запрошен.
    assert delivered * len(chunk) <= MAX_BODY_BYTES + len(chunk)
    assert delivered < total_chunks
    assert (await session.scalars(select(TelegramUpdateInbox))).all() == []


@pytest.mark.asyncio
async def test_privileged_database_role_keeps_the_update_out_of_inbox(
    engine: AsyncEngine, schema_engine: AsyncEngine, session: AsyncSession
) -> None:
    # Дверь в интернет обязана делать ту же fail-closed проверку роли, что и
    # поллер/воркер/CLI: на owner-роли апдейт не попадает в INBOX, а ответ
    # НЕ 503 — «не сконфигурирован» замаскировало бы ошибку конфигурации.
    async def privileged_provider() -> TelegramWebhookRuntime | None:
        await assert_non_privileged_application_role(schema_engine)
        return TelegramWebhookRuntime(
            secret=SECRET,
            bot_id=BOT_ID,
            max_body_bytes=MAX_BODY_BYTES,
            session_factory=create_session_factory(engine),
        )

    app = create_app(privileged_provider)

    response = await post_webhook(app, update_body(9), raise_app_exceptions=False)

    assert response.status_code == 500
    assert (await session.scalars(select(TelegramUpdateInbox))).all() == []


@pytest.mark.asyncio
async def test_environment_runtime_refuses_a_privileged_database_role(
    isolated_database: IsolatedDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Реальный провайдер (env) проверяет роль ДО первого enqueue.
    monkeypatch.setenv("DATABASE_URL", isolated_database.schema_database_url)
    monkeypatch.setenv("SCHEMA_DATABASE_URL", isolated_database.database_url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"{BOT_ID}:webhook-test-token")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER", "webhook-test-pepper")
    monkeypatch.setenv("INVITE_TOKEN_PEPPER_KEY_ID", "webhook-test-v1")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)

    with pytest.raises(RuntimeError, match="non-superuser"):
        await _webhook_runtime_from_environment()


@pytest.mark.asyncio
async def test_malformed_payload_responds_4xx_not_500(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    app = webhook_app(engine)

    broken_json = await post_webhook(app, b"{not json")
    not_object = await post_webhook(app, b'["update_id", 6]')
    missing_id = await post_webhook(app, b'{"message": {}}')
    text_id = await post_webhook(app, b'{"update_id": "seven"}')
    bool_id = await post_webhook(app, b'{"update_id": true}')

    for response in (broken_json, not_object, missing_id, text_id, bool_id):
        assert response.status_code == 400
    assert (await session.scalars(select(TelegramUpdateInbox))).all() == []


@pytest.mark.asyncio
async def test_database_failure_responds_500_for_telegram_retry() -> None:
    unreachable = create_database_engine(
        "postgresql+asyncpg://second_brain_app@127.0.0.1:1/second_brain"
    )
    runtime = TelegramWebhookRuntime(
        secret=SECRET,
        bot_id=BOT_ID,
        max_body_bytes=MAX_BODY_BYTES,
        session_factory=create_session_factory(unreachable),
    )
    app = create_app(constant_provider(runtime))
    try:
        response = await post_webhook(app, update_body(8))
    finally:
        await unreachable.dispose()

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_health_route_is_untouched_by_webhook_wiring() -> None:
    transport = httpx.ASGITransport(app=create_app(constant_provider(None)))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

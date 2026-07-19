"""Композиция FastAPI-приложения: /health, Telegram-webhook и /v1 (эпик API-1).

Webhook-роут (B1) — «insert-then-200»: сверка секрета из заголовка
``X-Telegram-Bot-Api-Secret-Token``, cap тела, минимальная проверка формы и
ОДИН идемпотентный INSERT в telegram_update_inbox. Никакой обработки в
HTTP-запросе: обработку ведёт inbox-шаг воркера (telegram_inbox_step).
Тело запроса и payload НИКОГДА не логируются (PII), путь статический, без
секрета в URL.

Роутер `/v1` (C1) — публичный API по персональному токену; его устройство и
конверт ошибок описаны в ``api_v1``.

В схеме OpenAPI остаётся ТОЛЬКО `/v1`. `/health` и `/telegram/webhook` помечены
``include_in_schema=False`` намеренно: схема — контракт для клиентов публичного
API, а эти два роута клиентам не адресованы. Webhook — дверь одного вызывающего
(Telegram), его форму диктует Telegram, и печатать её в публичной схеме значит
приглашать в неё стучаться; `/health` — проба контейнера и traefik, а не
возможность продукта. Оба продолжают работать как прежде — не описаны, но живы.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hmac import compare_digest

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.bootstrap.api_v1 import (
    ApiRuntimeProvider,
    create_v1_router,
    register_v1_error_handlers,
)
from second_brain.bootstrap.settings import Settings
from second_brain.shared.trace import TraceContext
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.inbox import (
    PostgresTelegramInboxQueue,
)

SECRET_TOKEN_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@dataclass(frozen=True)
class TelegramWebhookRuntime:
    """Собранные зависимости webhook-роута (секрет — вне repr/логов)."""

    secret: str = field(repr=False)
    bot_id: int
    max_body_bytes: int
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)


# None = webhook не сконфигурирован (нет секрета/окружения) → роут отвечает 503.
# Сборка асинхронная: до выдачи runtime проверяется роль БД.
WebhookRuntimeProvider = Callable[[], Awaitable[TelegramWebhookRuntime | None]]


async def _webhook_runtime_from_environment() -> TelegramWebhookRuntime | None:
    """Ленивая сборка зависимостей роута из env (на первом запросе).

    Ленивая, потому что ``main.py`` создаёт приложение на импорте, а health
    и тесты не обязаны иметь полное окружение. Любая нехватка конфигурации —
    честное «webhook не сконфигурирован» (503), не 500.

    Роль БД проверяется здесь, ДО первого enqueue, так же как это делают
    поллер, воркер и CLI: webhook — единственная дверь, смотрящая в интернет,
    и работать с owner/superuser/BYPASSRLS-ролью она не смеет. Привилегированная
    роль — не «выключенный webhook», а ошибка конфигурации, поэтому RuntimeError
    идёт наружу (500 и причина в логе сервера), а 503 её бы замаскировал.
    """
    try:
        settings = Settings.from_environment()
        bot_id = settings.telegram_bot_id()
    except RuntimeError:
        return None
    if settings.telegram_webhook_secret is None:
        return None
    engine = create_database_engine(settings.database_url)
    try:
        await assert_non_privileged_application_role(engine)
    except RuntimeError:
        # Движок в кэш не попадёт — закрываем пул, иначе каждая попытка на
        # неверной роли оставляла бы висячие соединения.
        await engine.dispose()
        raise
    return TelegramWebhookRuntime(
        secret=settings.telegram_webhook_secret,
        bot_id=bot_id,
        max_body_bytes=settings.webhook_max_body_bytes,
        session_factory=create_session_factory(engine),
    )


def create_app(
    webhook_runtime_provider: WebhookRuntimeProvider | None = None,
    api_runtime_provider: ApiRuntimeProvider | None = None,
) -> FastAPI:
    """Создаёт приложение; provider позволяет тестам подменить зависимости."""
    app = FastAPI(title="Second Brain", version="0.1.0")
    provider = webhook_runtime_provider or _webhook_runtime_from_environment
    # Кэш собранных зависимостей: env в процессе не меняется, пересборка
    # engine (и проверки роли БД) на каждый запрос недопустима. Кэшируется и
    # None (503 навсегда до рестарта с настроенным секретом), но НЕ сбой
    # проверки роли: он уходит наружу, а не оседает в кэше. Замок нужен из-за
    # await внутри сборки — без него параллельные первые запросы собрали бы
    # по своему engine.
    runtime_cache: list[TelegramWebhookRuntime | None] = []
    runtime_lock = asyncio.Lock()

    async def resolve_runtime() -> TelegramWebhookRuntime | None:
        async with runtime_lock:
            if not runtime_cache:
                runtime_cache.append(await provider())
            return runtime_cache[0]

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/telegram/webhook", include_in_schema=False)
    async def telegram_webhook(request: Request) -> Response:
        runtime = await resolve_runtime()
        if runtime is None:
            return Response(status_code=503)
        header_secret = request.headers.get(SECRET_TOKEN_HEADER, "")
        if not compare_digest(header_secret.encode(), runtime.secret.encode()):
            # 401 без тела; сам присланный секрет никуда не пишется.
            return Response(status_code=401)
        declared_length = request.headers.get("content-length", "")
        if declared_length.isdigit() and int(declared_length) > runtime.max_body_bytes:
            return Response(status_code=413)
        # Content-Length может и не быть (chunked), поэтому тело читается по
        # частям и режется на первом же чанке за лимитом: остаток не
        # вычитывается, лишнее в память не попадает.
        buffer = bytearray()
        async for chunk in request.stream():
            buffer.extend(chunk)
            if len(buffer) > runtime.max_body_bytes:
                return Response(status_code=413)
        body = bytes(buffer)
        try:
            payload = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        update_id = payload.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            return Response(status_code=400)
        try:
            async with runtime.session_factory() as session, session.begin():
                await PostgresTelegramInboxQueue(session).enqueue(
                    bot_id=runtime.bot_id,
                    update_id=update_id,
                    payload=payload,
                    received_at=datetime.now(UTC),
                    trace_id=TraceContext.new_root().trace_id,
                )
        except Exception:
            # 500 без деталей (payload в ответ/лог не попадает): Telegram
            # ретраит, повтор погасится конфликтом (bot_id, update_id).
            return Response(status_code=500)
        return JSONResponse({"ok": True})

    app.include_router(create_v1_router(api_runtime_provider))
    register_v1_error_handlers(app)
    return app

"""Публичный HTTP-API `/v1`: вход по токену и конверт ошибок (эпик API-1, C1).

Здесь только фундамент: проверка предъявленного токена на входе, единый конверт
ошибок и ОДИН эндпоинт-доказательство ``GET /v1/me``. Содержательные эндпоинты
(записи, поиск, сводка, экспорт) — следующие слайсы эпика; контракт `/v1` пока
черновой и будет заморожен в его конце.

Устройство цепочки, ради которого всё и написано:

1. клиент предъявляет ``Authorization: Bearer <секрет>``;
2. ``AuthenticateApiToken`` находит по секрету живой токен и говорит, чей он;
3. ответ этой проверки — ЕДИНСТВЕННЫЙ источник того, чья это память;
4. дальше работа идёт в сессии со scope этого пространства (``scoped_session``),
   то есть чужие строки не видит уже сама база, а не только наш SELECT.

Чужое пространство недостижимо не потому, что мы проверяем присланный
идентификатор, а потому, что мы его НЕ ЧИТАЕМ: ни из тела, ни из пути, ни из
query-параметров роутер пространство/пользователя не берёт вовсе. Подделывать
здесь нечего — параметра, который на это влияет, не существует.

Секрет токена не логируется, не попадает в repr, в текст ошибки, в конверт и в
примеры OpenAPI: наружу уходит только безопасный код ошибки и идентификатор
трассировки, по которому запрос ищется в логах сервиса.

Конвенция контракта: все моменты времени — UTC в формате RFC3339.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ExceptionHandler

from second_brain.bootstrap.settings import Settings
from second_brain.shared.clock import Clock, SystemClock
from second_brain.shared.i18n import resolve_locale
from second_brain.shared.trace import TraceContext
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
    set_user_space_scope,
)
from second_brain.slices.identity.adapters.persistence.models import UserSpace
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresApiTokenRepository,
)
from second_brain.slices.identity.application.api_tokens import AuthenticateApiToken
from second_brain.slices.identity.application.contracts import AccessContext

API_PREFIX = "/v1"
TRACE_ID_HEADER = "X-Trace-Id"

# Коды ошибок конверта. Намеренно грубые: код говорит клиенту, что делать
# (перевыдать токен, подождать, повторить), и НИЧЕГО не говорит о том, что
# именно у нас внутри пошло не так.
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_TOO_MANY_REQUESTS = "too_many_requests"
ERROR_NOT_FOUND = "not_found"
ERROR_METHOD_NOT_ALLOWED = "method_not_allowed"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_UNAVAILABLE = "unavailable"
ERROR_INTERNAL = "internal"

_STATUS_ERROR_CODES = {
    401: ERROR_UNAUTHORIZED,
    404: ERROR_NOT_FOUND,
    405: ERROR_METHOD_NOT_ALLOWED,
    429: ERROR_TOO_MANY_REQUESTS,
    503: ERROR_UNAVAILABLE,
}

# Логгер сервиса: сюда пишется trace_id, по которому владелец находит запрос.
# Ни секрета, ни query string (в ней могут оказаться личные данные), ни текста
# исключения в сообщении нет — детали исключения уходят в exc_info, то есть в
# лог сервера, и никогда в ответ.
_logger = logging.getLogger("second_brain.api")

_bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "Персональный токен доступа, выданный кнопкой «🔑 API» в боте. "
        "Токен определяет, ЧЬЯ память отвечает на запрос."
    ),
)


class ErrorBody(BaseModel):
    """Тело ошибки: безопасный код и идентификатор трассировки."""

    code: str = Field(examples=[ERROR_UNAUTHORIZED])
    trace_id: str = Field(
        description="Идентификатор запроса; по нему запрос ищется в логах сервиса.",
        examples=["0f3c8a1d5e7b4c2a9d6f8e1b3a5c7d9e"],
    )


class ErrorEnvelope(BaseModel):
    """Единый конверт ошибки для всего `/v1`."""

    error: ErrorBody


# Докстроки моделей и обработчиков уезжают в опубликованную схему, то есть их
# читает клиент, а не мы. Поэтому здесь — что эндпоинт даёт, а внутренняя
# мотивация («зачем он вообще заведён») живёт в обычных комментариях рядом.
class MeResponse(BaseModel):
    """Кем распознан вызывающий: пользователь, его пространство и его настройки."""

    user_id: UUID
    user_space_id: UUID
    language: str = Field(
        description="Действующий язык пространства (пока язык не выбран — ru).",
        examples=["ru"],
    )
    timezone: str = Field(examples=["Asia/Jerusalem"])


class ApiError(Exception):
    """Ошибка `/v1`, которую можно показать наружу: только код и трассировка."""

    def __init__(self, status_code: int, code: str, trace: TraceContext) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.trace = trace


@dataclass(frozen=True)
class ApiRuntime:
    """Собранные зависимости `/v1` (перец живёт внутри authenticate, вне repr)."""

    authenticate: AuthenticateApiToken = field(repr=False)
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)
    clock: Clock
    # 0 = лимит на провалы выключен.
    failure_limit: int
    failure_window: timedelta
    # None = заголовкам не верим, адрес берём из сокета (см. client_address).
    client_ip_header: str | None


# None = API не сконфигурирован (нет окружения) → роутер отвечает 503.
ApiRuntimeProvider = Callable[[], Awaitable[ApiRuntime | None]]


@dataclass(frozen=True)
class AuthenticatedCaller:
    """Кто пришёл и с каким runtime его обслуживать."""

    access_context: AccessContext
    trace: TraceContext
    runtime: ApiRuntime = field(repr=False)


class AuthorizationFailureLimiter:
    """Скользящее окно ПРОВАЛОВ авторизации на адрес — против подбора токена.

    Считаются только провалы: удачный запрос бюджет не тратит, поэтому обычной
    работе лимит не мешает вовсе. Это второй рубеж — первый (общий лимит частоты
    на traefik) стоит раньше и не знает, чем кончилась проверка токена.

    Счётчики живут в памяти процесса: сервис `api` один, и переживать рестарт
    такому счётчику незачем — после рестарта подбор начинается заново, но и сам
    подбор от рестарта не ускоряется.
    """

    # Порог, после которого делается полная уборка протухших адресов. Нужен
    # ровно против того, от чего лимит и защищает: распределённый перебор иначе
    # растил бы словарь без края.
    _SWEEP_THRESHOLD = 1024

    def __init__(self, limit: int, window: timedelta) -> None:
        self._limit = limit
        self._window = window
        self._failures: dict[str, list[datetime]] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def is_blocked(self, address: str, now: datetime) -> bool:
        if not self.enabled:
            return False
        return len(self._recent(address, now)) >= self._limit

    def register_failure(self, address: str, now: datetime) -> None:
        if not self.enabled:
            return
        recent = self._recent(address, now)
        recent.append(now)
        self._failures[address] = recent
        if len(self._failures) > self._SWEEP_THRESHOLD:
            self._sweep(now)

    def _recent(self, address: str, now: datetime) -> list[datetime]:
        threshold = now - self._window
        return [
            moment for moment in self._failures.get(address, ()) if moment > threshold
        ]

    def _sweep(self, now: datetime) -> None:
        threshold = now - self._window
        self._failures = {
            address: moments
            for address, moments in self._failures.items()
            if any(moment > threshold for moment in moments)
        }


def client_address(request: Request, forwarded_header: str | None) -> str:
    """Адрес клиента для лимита провалов.

    Честного источника ровно два, и оба надо называть своими именами.

    ``forwarded_header`` не задан — берём адрес сокета. Это правда только там,
    где до приложения никто не стоит: за обратным прокси в сокете окажется сам
    прокси, и лимит на адрес выродится в один общий лимит на всех.

    ``forwarded_header`` задан — берём ПРАВУЮ запись списка. Её дописал
    ближайший прокси, увидевший настоящий сокет; всё левее прислал сам клиент и
    подделывается свободно. Включать этот режим можно только там, где до
    приложения не достучаться мимо прокси, иначе клиент сам себе назначит адрес.
    """
    if forwarded_header:
        raw = request.headers.get(forwarded_header)
        if raw:
            nearest = raw.rsplit(",", 1)[-1].strip()
            if nearest:
                return nearest
    client = request.client
    return client.host if client is not None else "unknown"


@asynccontextmanager
async def scoped_session(
    session_factory: async_sessionmaker[AsyncSession],
    access_context: AccessContext,
) -> AsyncIterator[AsyncSession]:
    """Транзакция, в которой БАЗА знает пространство вызывающего.

    Тот же механизм, что и на бот-пути: транзакционный ``set_config`` под
    политики RLS. Именно он, а не аккуратность отдельного SELECT'а, делает чужие
    строки недостижимыми для содержательных эндпоинтов следующих слайсов.
    """
    async with session_factory() as session:
        async with session.begin():
            await set_user_space_scope(session, access_context)
            yield session


async def _api_runtime_from_environment() -> ApiRuntime | None:
    """Ленивая сборка зависимостей `/v1` из env (на первом запросе).

    Ленивая по той же причине, что и у webhook: ``main.py`` создаёт приложение
    на импорте, а `/health` и тесты не обязаны иметь полное окружение. Нехватка
    конфигурации — честное «API не сконфигурирован» (503), не 500.

    Роль БД проверяется здесь, ДО первого запроса к данным: `/v1` — дверь,
    смотрящая в интернет, и работать с owner/superuser/BYPASSRLS-ролью она не
    смеет (на такой роли политики RLS не применяются вовсе). Привилегированная
    роль — ошибка конфигурации, а не «выключенный API», поэтому RuntimeError
    идёт наружу, а 503 её бы замаскировал.
    """
    try:
        settings = Settings.from_environment()
    except RuntimeError:
        return None
    engine = create_database_engine(settings.database_url)
    try:
        await assert_non_privileged_application_role(engine)
    except Exception:
        # Движок в кэш не попадёт — закрыть его больше некому, поэтому пул
        # закрывается на ЛЮБОМ сбое проверки, а не только на неверной роли:
        # недоступная база и таймаут выглядят иначе (ошибка драйвера), но
        # оставленные соединения от этого не перестают копиться.
        await engine.dispose()
        raise
    session_factory = create_session_factory(engine)
    clock = SystemClock()
    return ApiRuntime(
        authenticate=AuthenticateApiToken(
            repository=PostgresApiTokenRepository(session_factory),
            clock=clock,
            pepper=settings.api_token_pepper,
            pepper_key_id=settings.api_token_pepper_key_id,
            last_used_throttle=timedelta(
                seconds=settings.api_token_last_used_throttle_seconds
            ),
        ),
        session_factory=session_factory,
        clock=clock,
        failure_limit=settings.api_auth_failure_limit,
        failure_window=timedelta(seconds=settings.api_auth_failure_window_seconds),
        client_ip_header=settings.api_client_ip_header,
    )


def create_v1_router(provider: ApiRuntimeProvider | None = None) -> APIRouter:
    """Собирает роутер `/v1`; provider позволяет тестам подменить зависимости."""
    router = APIRouter(prefix=API_PREFIX, tags=["v1"])
    resolve = _lazy_runtime(provider or _api_runtime_from_environment)
    limiter_cache: list[AuthorizationFailureLimiter] = []

    def limiter(runtime: ApiRuntime) -> AuthorizationFailureLimiter:
        # Один счётчик на роутер: runtime закэширован, значит порог и окно у
        # него уже не меняются.
        if not limiter_cache:
            limiter_cache.append(
                AuthorizationFailureLimiter(
                    limit=runtime.failure_limit, window=runtime.failure_window
                )
            )
        return limiter_cache[0]

    async def authenticated_caller(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
        ],
    ) -> AuthenticatedCaller:
        """Единственный вход в `/v1`: кто предъявил токен — тот и распознан.

        Идентификатор пользователя и пространства берутся ТОЛЬКО отсюда. Тело,
        путь и query-параметры на выбор пространства не влияют и не читаются:
        подделать нечего.

        Любой провал — нет заголовка, кривой формат, неизвестный, отозванный
        токен, деактивированный пользователь — отвечает ОДИНАКОВЫМ 401. Разное
        поведение подсказало бы, какой токен существует.

        Время ответа на 401 при этом разное: пустой или кривой заголовок
        отваливается сразу, непустой идёт через хэш и чтение. Выравнивать это
        холостым обращением к базе незачем — оракула тут нет: вызывающий и так
        знает, что именно прислал, а «неизвестный», «отозванный» и
        «деактивированный» между собой не различаются ни ответом, ни временем.
        """
        trace = TraceContext.new_root()
        try:
            runtime = await resolve()
        except Exception:
            # Сборка зависимостей ленивая, то есть происходит ВНУТРИ первого
            # запроса: недоступная база или сорванная проверка роли здесь —
            # такая же беда `/v1`, как и любая другая, и уйти наружу она должна
            # нашим конвертом с трассировкой, а не голым ответом сервера.
            _logger.error(
                "api runtime setup failed trace_id=%s", trace.trace_id, exc_info=True
            )
            raise ApiError(500, ERROR_INTERNAL, trace) from None
        if runtime is None:
            raise ApiError(503, ERROR_UNAVAILABLE, trace)
        address = client_address(request, runtime.client_ip_header)
        now = runtime.clock.now()
        failures = limiter(runtime)
        # Бюджет спрашивается заранее, но НЕ закрывает дверь до проверки токена.
        # Иначе один перебирающий запирал бы снаружи всех: по умолчанию адрес
        # берётся из сокета, а за прокси он у всех вызывающих один и тот же —
        # исчерпав общий бюджет, злоумышленник отказал бы в обслуживании и
        # владельцу живого токена. Цена размена — одно чтение по индексу хэша
        # на попытку сверх бюджета; общий поток до этого места уже ограничен
        # лимитом частоты на traefik.
        blocked = failures.is_blocked(address, now)

        def deny() -> ApiError:
            """Отказ на провале: исчерпанный бюджет — 429, живой — 401 и списание."""
            if blocked:
                # Списывать нечего: бюджет уже исчерпан, а продлевать блокировку
                # каждым новым отказом значит не отпускать адрес никогда.
                return ApiError(429, ERROR_TOO_MANY_REQUESTS, trace)
            failures.register_failure(address, now)
            return ApiError(401, ERROR_UNAUTHORIZED, trace)

        secret = credentials.credentials if credentials is not None else ""
        if not secret:
            raise deny()
        try:
            principal = await runtime.authenticate.execute(secret)
        except Exception:
            # Сбой базы — НЕ провал авторизации: 401 здесь соврал бы владельцу
            # живого токена, а чужая авария съела бы его бюджет попыток.
            _logger.error(
                "api token check failed trace_id=%s", trace.trace_id, exc_info=True
            )
            raise ApiError(500, ERROR_INTERNAL, trace) from None
        if principal is None:
            raise deny()
        # Валидный токен проходит и при исчерпанном бюджете: провалов он не
        # делает, а значит и лимит на провалы к нему отношения не имеет.
        return AuthenticatedCaller(
            access_context=principal.access_context, trace=trace, runtime=runtime
        )

    @router.get(
        "/me",
        summary="Кем распознан вызывающий",
        response_model=MeResponse,
        responses=_error_responses(401, 429, 500, 503),
    )
    async def read_me(
        caller: Annotated[AuthenticatedCaller, Depends(authenticated_caller)],
    ) -> MeResponse:
        """Отдаёт пользователя, его пространство, язык и часовой пояс."""
        # Заведён как проба всей цепочки «токен → пространство → данные»:
        # отвечает он мало, но отвечает только тогда, когда работает вход
        # целиком. Клиенту эта мотивация не нужна, поэтому она не в докстроке.
        access_context = caller.access_context
        try:
            async with scoped_session(
                caller.runtime.session_factory, access_context
            ) as session:
                row = (
                    await session.execute(
                        # Owner-предикат, как и на бот-пути: на user_spaces RLS
                        # нет, изоляция здесь — «своё пространство своего
                        # владельца».
                        select(UserSpace.language, UserSpace.timezone).where(
                            UserSpace.id == access_context.user_space_id,
                            UserSpace.owner_user_id == access_context.user_id,
                        )
                    )
                ).one_or_none()
        except Exception:
            _logger.error(
                "api /v1/me failed trace_id=%s", caller.trace.trace_id, exc_info=True
            )
            raise ApiError(500, ERROR_INTERNAL, caller.trace) from None
        if row is None:
            # Недостижимо: токен уже сшит с живым пространством живого
            # пользователя. Если случилось — это наша поломка, не ошибка клиента.
            _logger.error(
                "api /v1/me found no space trace_id=%s", caller.trace.trace_id
            )
            raise ApiError(500, ERROR_INTERNAL, caller.trace)
        language, timezone = row
        return MeResponse(
            user_id=access_context.user_id,
            user_space_id=access_context.user_space_id,
            language=resolve_locale(language).value,
            timezone=timezone,
        )

    return router


def register_v1_error_handlers(app: FastAPI) -> None:
    """Вешает на приложение единый конверт ошибок — только для путей `/v1`.

    Всё, что вне `/v1` (webhook, `/health`), обрабатывается как раньше: их
    ответы — контракт Telegram и инфраструктуры, а не публичного API.
    """

    async def handle_api_error(request: Request, error: ApiError) -> Response:
        _logger.warning(
            "api error status=%s code=%s trace_id=%s path=%s",
            error.status_code,
            error.code,
            error.trace.trace_id,
            request.url.path,
        )
        return error_response(error.status_code, error.code, error.trace)

    async def handle_http_exception(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        if not request.url.path.startswith(f"{API_PREFIX}/"):
            return await http_exception_handler(request, error)
        trace = TraceContext.new_root()
        code = _STATUS_ERROR_CODES.get(
            error.status_code,
            ERROR_INTERNAL if error.status_code >= 500 else ERROR_INVALID_REQUEST,
        )
        _logger.warning(
            "api error status=%s code=%s trace_id=%s path=%s",
            error.status_code,
            code,
            trace.trace_id,
            request.url.path,
        )
        return error_response(error.status_code, code, trace)

    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> Response:
        """Ошибка разбора параметров/тела под `/v1` — тоже наш конверт.

        Стоит здесь, а не появится вместе с первым эндпоинтом, у которого есть
        параметры: `/v1` — фундамент всей поверхности, и следующий билдер не
        должен помнить про этот угол. Иначе первый же параметр вернул бы ответ
        FastAPI по умолчанию — со списком ``detail``, где видно имена полей и
        куски присланного, то есть ровно то, чего конверт не показывает.
        """
        if not request.url.path.startswith(f"{API_PREFIX}/"):
            return await request_validation_exception_handler(request, error)
        trace = TraceContext.new_root()
        _logger.warning(
            "api error status=%s code=%s trace_id=%s path=%s",
            422,
            ERROR_INVALID_REQUEST,
            trace.trace_id,
            request.url.path,
        )
        return error_response(422, ERROR_INVALID_REQUEST, trace)

    # cast: Starlette типизирует обработчик как принимающий базовый Exception,
    # регистрируя его при этом по конкретному классу — сузить тип иначе нечем.
    app.add_exception_handler(ApiError, cast(ExceptionHandler, handle_api_error))
    app.add_exception_handler(
        StarletteHTTPException, cast(ExceptionHandler, handle_http_exception)
    )
    app.add_exception_handler(
        RequestValidationError, cast(ExceptionHandler, handle_validation_error)
    )


def error_response(status_code: int, code: str, trace: TraceContext) -> JSONResponse:
    """Конверт ошибки: код + трассировка, и больше НИЧЕГО.

    Ни текста исключения, ни SQL, ни путей файлов, ни предъявленного секрета —
    наружу уходит только то, что клиенту можно знать.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "trace_id": trace.trace_id}},
        headers={TRACE_ID_HEADER: trace.trace_id},
    )


def _error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {
        status: {"model": ErrorEnvelope, "description": _STATUS_DESCRIPTIONS[status]}
        for status in statuses
    }


_STATUS_DESCRIPTIONS = {
    401: "Токен не предъявлен, неизвестен, отозван или больше не действует.",
    429: "Слишком много неудачных попыток авторизации с этого адреса.",
    500: "Внутренняя ошибка; подробности — только в логах сервиса по trace_id.",
    503: "API не сконфигурирован на этом развёртывании.",
}


def _lazy_runtime(provider: ApiRuntimeProvider) -> ApiRuntimeProvider:
    """Кэш собранных зависимостей: env в процессе не меняется.

    Пересобирать engine (и проверку роли БД) на каждый запрос недопустимо.
    Кэшируется и None (503 до рестарта с настроенным окружением), но НЕ сбой
    сборки: он уходит наружу, а не оседает в кэше, — следующий запрос попробует
    ещё раз (база могла и подняться), а вызывающий получит его нашим конвертом.
    Замок нужен из-за await внутри сборки — без него параллельные первые запросы
    собрали бы по своему engine.
    """
    cache: list[ApiRuntime | None] = []
    lock = asyncio.Lock()

    async def resolve() -> ApiRuntime | None:
        async with lock:
            if not cache:
                cache.append(await provider())
            return cache[0]

    return resolve

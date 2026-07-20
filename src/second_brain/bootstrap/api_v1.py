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

Осознанная ошибка эндпоинта поднимается как ``ApiError`` со СВОИМ кодом —
``handle_api_error`` отдаёт его дословно. Таблица ``_STATUS_ERROR_CODES`` к
этому отношения не имеет: она нужна только там, где статус придумал не мы, а
фреймворк (несуществующий путь, неверный метод). Поднять «голый»
``HTTPException`` ради нового кода нельзя — оттуда он выйдет с кодом из этой
таблицы или её умолчанием, а не с тем, который задумывался.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, ExceptionHandler, Message, Receive, Scope, Send

from second_brain.bootstrap.settings import (
    DEFAULT_API_WRITE_RATE_LIMIT,
    DEFAULT_API_WRITE_RATE_WINDOW_SECONDS,
    Settings,
)
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.shared.clock import Clock, SystemClock
from second_brain.shared.i18n import resolve_locale
from second_brain.shared.trace import TraceContext
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    TelegramLink,
)
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
    set_user_space_scope,
)
from second_brain.slices.identity.adapters.persistence.models import UserSpace
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresApiTokenRepository,
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.api_tokens import AuthenticateApiToken
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.tasks.domain.entities import PendingCaptureType
from second_brain.slices.weblinks.adapters.normalization import normalize_url

API_PREFIX = "/v1"
TRACE_ID_HEADER = "X-Trace-Id"

# Границы полей записи. Текст ограничен под cap тела (64 КиБ ≈ 32 000 знаков
# по-русски в UTF-8), чтобы слишком длинная заметка была внятным 422 с указанием
# поля, а не безымянным «запрос слишком большой».
CAPTURE_TEXT_MAX_LENGTH = 32_000
# client_ref ограничен, потому что он ложится в btree-индекс: кортеж индекса не
# бывает длиннее ~2704 байт и не жмётся, поэтому строка в пару тысяч знаков
# (влезающая в тело!) прошла бы проверку и умерла бы на INSERT'е — то есть 500
# на КАЖДЫЙ повтор этого запроса, навсегда. Формат не навязываем: какой у
# устройства идентификатор — решать владельцу, а не нам.
CLIENT_REF_MAX_LENGTH = 128
_LINK_URL_MAX_LENGTH = 2_048
# Второй потолок того же адреса, и он не дублирует первый. Знаковый выше — про
# присланное; этот — про НОРМАЛИЗОВАННОЕ, потому что в btree-индекс
# uq_page_titles_space_normalized_url ложится именно normalized_url, а кортеж
# индекса меряется в БАЙТАХ (~2704) и длиннее не бывает.
#
# Мерить байты присланного было бы мало: нормализация не только режет (фрагмент,
# дефолтный порт), но и УДЛИНЯЕТ — IDNA раздувает не-ASCII хост в разы
# (é → xn--9ca, 2 байта → 7), и адрес, влезавший сырым, выходит из неё вдвое
# длиннее. Поэтому меряется ровно то, что доедет до индекса.
#
# Потолок взят по НЕсжатой длине намеренно. Индекс жмёт значение перед укладкой,
# поэтому повторяющийся адрес и в 3 КБ проходит, а несжимаемый той же длины даёт
# 500 — и так на КАЖДОМ повторе, навсегда. Порог, зависящий от того, что за буквы
# внутри, — это не порог.
_LINK_NORMALIZED_URL_MAX_BYTES = 2_048

# Коды ошибок конверта. Намеренно грубые: код говорит клиенту, что делать
# (перевыдать токен, подождать, повторить), и НИЧЕГО не говорит о том, что
# именно у нас внутри пошло не так.
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_TOO_MANY_REQUESTS = "too_many_requests"
ERROR_NOT_FOUND = "not_found"
ERROR_METHOD_NOT_ALLOWED = "method_not_allowed"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_PAYLOAD_TOO_LARGE = "payload_too_large"
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


def _reject_nul(value: str) -> str:
    """Отбить U+0000: контракт его принимал, PostgreSQL — нет.

    В text/varchar нулевого байта не бывает вовсе, поэтому строка с ним умирает
    на INSERT'е и откатывает весь захват. Наружу это уходило бы как 500, а 500
    для очереди клиента значит «повтори позже» — то есть такая запись не прошла
    бы НИКОГДА и заперла бы за собой всю очередь. Отказ обязан быть внятным
    отказом входу (422), а не обещанием, что позже получится.
    """
    if "\x00" in value:
        raise ValueError("value must not contain NUL")
    return value


class CaptureLinkBody(BaseModel):
    """Ссылка при записи: пара «слово → адрес», как в сообщении бота."""

    # У слова потолка нет: оно лежит в Text и ни в какой индекс не идёт, границу
    # ему ставит cap тела. Свой потолок только отказывал бы в законном.
    label: str
    url: str = Field(max_length=_LINK_URL_MAX_LENGTH)

    @field_validator("label", "url")
    @classmethod
    def _must_not_contain_nul(cls, value: str) -> str:
        return _reject_nul(value)

    @field_validator("url")
    @classmethod
    def _url_must_fit_the_page_title_index(cls, value: str) -> str:
        normalized = normalize_url(value)
        # None — адрес не канонизируется, в очередь титулов он не ставится
        # вовсе: до индекса не доедет ничего, мерить нечего.
        if (
            normalized is not None
            and len(normalized.encode()) > _LINK_NORMALIZED_URL_MAX_BYTES
        ):
            raise ValueError("url is too long once normalized")
        return value


class CaptureRequest(BaseModel):
    """Что прислать, чтобы запись появилась."""

    text: str = Field(
        min_length=1,
        max_length=CAPTURE_TEXT_MAX_LENGTH,
        description=(
            "Текст записи ДОСЛОВНО — как прислали, так и сохраним. Пустой текст "
            "и текст из одних пробелов не принимаются."
        ),
    )
    client_ref: str = Field(
        min_length=1,
        max_length=CLIENT_REF_MAX_LENGTH,
        description=(
            "Ключ повтора: своё значение на КАЖДОЕ действие пользователя. "
            "Повтор с тем же значением вернёт ответ первого вызова и НИЧЕГО не "
            "создаст; тела при этом не сравниваются. Не берите его из текста "
            "(хэш содержимого склеил бы две разные записи одних и тех же слов)."
        ),
    )
    tz: str = Field(
        description=(
            "Часовой пояс запроса (имя IANA). Им разбирается относительное "
            "время («завтра в 9»). На формат ответа не влияет: все моменты "
            "времени наружу уходят в UTC."
        ),
        examples=["Europe/Lisbon"],
    )
    type: PendingCaptureType | None = Field(
        default=None,
        description=(
            "Тип записи. Назван — он главнее времени; не назван — заметка, "
            "которая при явном будущем времени становится задачей с "
            "напоминанием."
        ),
    )
    modality: Literal["text", "voice_transcript"] = Field(
        default="text",
        description="Откуда взялся текст: набран или надиктован. Пока пометка.",
    )
    links: tuple[CaptureLinkBody, ...] = ()

    @field_validator("text")
    @classmethod
    def _text_must_not_be_blank(cls, value: str) -> str:
        # Проверяем обрезанное, храним присланное. Текст из одних пробелов —
        # это задетый в кармане телефон, а не запись; удалить её до появления
        # DELETE будет нечем, и она навсегда осядет в поиске и сводке.
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("text", "client_ref")
    @classmethod
    def _must_not_contain_nul(cls, value: str) -> str:
        return _reject_nul(value)

    @field_validator("tz")
    @classmethod
    def _tz_must_be_a_real_zone(cls, value: str) -> str:
        # Проверяем на входе, чтобы мусор не дошёл до разбора времени: там
        # ZoneInfo поднимет исключение внутри запроса и клиент получит голый 500
        # вместо внятного «неверный запрос».
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("tz must be an IANA time zone name") from None
        return value


class CaptureRecordBody(BaseModel):
    """Запись, созданная ЭТИМ вызовом."""

    type: str = Field(examples=["task"])
    id: UUID


class CaptureResponse(BaseModel):
    """Итог записи: журнал, созданная запись и, если есть, момент напоминания.

    `record` — запись, созданную ЭТИМ вызовом. Позже к тому же захвату разбор
    текста может добавить ещё записи; про них этот ответ ничего не говорит —
    они видны в поиске и сводке.
    """

    capture_id: UUID
    record: CaptureRecordBody
    reminder_at: datetime | None = Field(
        default=None,
        description=(
            "Когда напомним, в UTC. Относительное время разбирается от момента "
            "ПРИЁМА запроса сервером, а не от момента, когда его набрали: "
            "очередь, слитая с опозданием, поставит напоминание от прихода — "
            "поэтому он здесь и виден."
        ),
    )
    request_tz: str = Field(
        description=(
            "Пояс, которым РЕАЛЬНО разобрано время этого захвата. На повторе "
            "это пояс ПЕРВОГО вызова, а не присланный сейчас."
        ),
        examples=["Europe/Lisbon"],
    )


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
    # 0 = бюджет записей на пространство выключен.
    write_rate_limit: int = DEFAULT_API_WRITE_RATE_LIMIT
    write_rate_window: timedelta = timedelta(
        seconds=DEFAULT_API_WRITE_RATE_WINDOW_SECONDS
    )


# None = API не сконфигурирован (нет окружения) → роутер отвечает 503.
ApiRuntimeProvider = Callable[[], Awaitable[ApiRuntime | None]]


@dataclass(frozen=True)
class AuthenticatedCaller:
    """Кто пришёл и с каким runtime его обслуживать."""

    access_context: AccessContext
    trace: TraceContext
    runtime: ApiRuntime = field(repr=False)


class SlidingWindowCounter:
    """Счётчик событий по ключу в скользящем окне — механика обоих лимитов.

    Заведён отдельно, потому что лимитов стало два, а механика у них одна:
    отсечь протухшее, сравнить с порогом, изредка подмести словарь. Смысл же у
    них разный (провалы авторизации на адрес против записей на пространство), и
    смешивать эти два смысла в одном классе было бы враньём — поэтому механика
    здесь, а имена и решения остались у каждого лимита свои.

    Счётчики живут в памяти процесса: сервис `api` один, и переживать рестарт
    такому счётчику незачем.
    """

    # Порог, после которого делается полная уборка протухших ключей. Нужен ровно
    # против того, от чего лимиты и защищают: распределённый перебор иначе растил
    # бы словарь без края.
    _SWEEP_THRESHOLD = 1024

    def __init__(self, limit: int, window: timedelta) -> None:
        self._limit = limit
        self._window = window
        self._events: dict[str, list[datetime]] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def is_exhausted(self, key: str, now: datetime) -> bool:
        if not self.enabled:
            return False
        return len(self._recent(key, now)) >= self._limit

    def register(self, key: str, now: datetime) -> None:
        if not self.enabled:
            return
        recent = self._recent(key, now)
        recent.append(now)
        self._events[key] = recent
        if len(self._events) > self._SWEEP_THRESHOLD:
            self._sweep(now)

    def _recent(self, key: str, now: datetime) -> list[datetime]:
        threshold = now - self._window
        return [moment for moment in self._events.get(key, ()) if moment > threshold]

    def _sweep(self, now: datetime) -> None:
        threshold = now - self._window
        self._events = {
            key: moments
            for key, moments in self._events.items()
            if any(moment > threshold for moment in moments)
        }


class AuthorizationFailureLimiter:
    """Скользящее окно ПРОВАЛОВ авторизации на адрес — против подбора токена.

    Считаются только провалы: удачный запрос бюджет не тратит, поэтому обычной
    работе лимит не мешает вовсе. Это второй рубеж — первый (общий лимит частоты
    на traefik) стоит раньше и не знает, чем кончилась проверка токена.

    Счётчики живут в памяти процесса: сервис `api` один, и переживать рестарт
    такому счётчику незачем — после рестарта подбор начинается заново, но и сам
    подбор от рестарта не ускоряется.
    """

    def __init__(self, limit: int, window: timedelta) -> None:
        self._counter = SlidingWindowCounter(limit, window)

    @property
    def enabled(self) -> bool:
        return self._counter.enabled

    def is_blocked(self, address: str, now: datetime) -> bool:
        return self._counter.is_exhausted(address, now)

    def register_failure(self, address: str, now: datetime) -> None:
        self._counter.register(address, now)


class WriteRateLimiter:
    """Бюджет ЗАПИСЕЙ на пространство — против зациклившегося приложения.

    Ключ — пространство, а не адрес и не токен. Телефон кочует между адресами,
    поэтому по адресу считать нечего; а два токена одного владельца ДОЛЖНЫ
    делить один бюджет — лимит бережёт данные пространства и процесс, а не
    отдельный ключ. Следствие честное и намеренное: зациклившийся ноутбук
    способен исчерпать бюджет, которым пользуется и телефон, — в продукте на
    одного владельца это и значит «бюджет на человека», а 429, показавший цикл,
    полезнее, чем цикл с личной квотой.
    """

    def __init__(self, limit: int, window: timedelta) -> None:
        self._counter = SlidingWindowCounter(limit, window)

    def check_and_spend(self, user_space_id: UUID, now: datetime) -> bool:
        """Списать одну запись; False = бюджет уже исчерпан."""
        key = str(user_space_id)
        if self._counter.is_exhausted(key, now):
            return False
        self._counter.register(key, now)
        return True


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
        write_rate_limit=settings.api_write_rate_limit,
        write_rate_window=timedelta(seconds=settings.api_write_rate_window_seconds),
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

    write_limiter_cache: list[WriteRateLimiter] = []

    def write_limiter(runtime: ApiRuntime) -> WriteRateLimiter:
        if not write_limiter_cache:
            write_limiter_cache.append(
                WriteRateLimiter(
                    limit=runtime.write_rate_limit, window=runtime.write_rate_window
                )
            )
        return write_limiter_cache[0]

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

    async def write_budget(
        caller: Annotated[AuthenticatedCaller, Depends(authenticated_caller)],
    ) -> AuthenticatedCaller:
        """Бюджет записей пространства — только на ПИШУЩИХ роутах.

        Здесь, а не внутри ``authenticated_caller``: тот общий со всеми
        чтениями, а бюджет спрашивали на запись. Зависимости решаются ДО разбора
        тела, поэтому бюджет тратит и запрос, чьё тело потом не прошло проверку:
        платим за попытку, а не за успех, — иначе кривой цикл разбирал бы тела
        бесплатно.
        """
        if not write_limiter(caller.runtime).check_and_spend(
            caller.access_context.user_space_id, caller.runtime.clock.now()
        ):
            raise ApiError(429, ERROR_TOO_MANY_REQUESTS, caller.trace)
        return caller

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

    @router.post(
        "/captures",
        summary="Записать мысль",
        status_code=201,
        response_model=CaptureResponse,
        responses={
            200: {
                "model": CaptureResponse,
                "description": (
                    "Повтор с уже использованным client_ref: возвращён ответ "
                    "ПЕРВОГО вызова, ничего нового не создано."
                ),
            },
            **_error_responses(401, 413, 422, 429, 500, 503),
        },
    )
    async def create_capture(
        request_body: CaptureRequest,
        response: Response,
        caller: Annotated[AuthenticatedCaller, Depends(write_budget)],
    ) -> CaptureResponse:
        """Создаёт запись из текста; повтор с тем же client_ref ничего не дублирует.

        Ответ 201 — запись создана, 200 — это повтор и вернулся ответ первого
        вызова. Тела повтора и первого вызова НЕ сравниваются: тот же
        client_ref с другим текстом вернёт первый захват, а новый текст
        потеряется — поэтому client_ref обязан быть своим на каждое действие
        пользователя.

        Момент приёма запроса сервером — база для относительного времени;
        собственную отметку времени клиент прислать не может.
        """
        # Мотивация, клиенту не нужная: запись — единственный путь, которым в
        # память попадает что-то не из телеграма, поэтому ветка происхождения и
        # ключ повтора живут именно здесь, а не в приложении-клиенте.
        access_context = caller.access_context
        received_at = datetime.now(UTC)
        try:
            async with scoped_session(
                caller.runtime.session_factory, access_context
            ) as session:
                capture_composition = TaskCaptureInTransaction()
                transaction = PostgresUpdateTransaction(session)
                stored = await capture_composition.claim_client_ref(
                    access_context, request_body.client_ref, transaction
                )
                if stored is not None:
                    answer = await _reconstruct_capture_answer(
                        session, access_context, stored, caller.trace
                    )
                    response.status_code = 200
                    return answer
                source = await capture_composition.capture(
                    CaptureTextCommand(
                        access_context=access_context,
                        channel="api",
                        raw_text=request_body.text,
                        received_at=received_at,
                        trace_id=caller.trace.trace_id,
                        client_ref=request_body.client_ref,
                        request_tz=request_body.tz,
                        modality=request_body.modality,
                        capture_type=request_body.type,
                        links=tuple(
                            TelegramLink(label=link.label, url=link.url)
                            for link in request_body.links
                        ),
                    ),
                    transaction,
                )
                return await _reconstruct_capture_answer(
                    session, access_context, source, caller.trace
                )
        except ApiError:
            raise
        except Exception:
            _logger.error(
                "api /v1/captures failed trace_id=%s",
                caller.trace.trace_id,
                exc_info=True,
            )
            raise ApiError(500, ERROR_INTERNAL, caller.trace) from None

    return router


async def _reconstruct_capture_answer(
    session: AsyncSession,
    access_context: AccessContext,
    capture: CaptureEvent,
    trace: TraceContext,
) -> CaptureResponse:
    """Собирает тело ответа из записанного — ОДИН путь и для 201, и для 200.

    Смысл client_ref в том, что слепой повтор получает ТОТ ЖЕ ответ. Повтор,
    вернувший другое тело, хуже, чем отсутствие идемпотентности вовсе: клиент
    такого не заметит. Поэтому 201 не собирается «из того, что под рукой» —
    оба ответа читаются отсюда, и разъехаться им негде. На первом вызове это
    чтение своих же записей внутри ещё открытой транзакции.

    Запись опознаётся ПЕРВИЧНЫМ прогоном обработки (версия 1), а не порядком по
    ``created_at``: разбор текста дописывает свои записи с тем же захватом, а
    стенные часы причинности не хранят.

    ``reminder_at`` читается по задаче, БЕЗ фильтра по статусу: повтор — это
    пере-выдача первого ответа, а не свежая сводка о напоминании. Иначе ответ
    менялся бы сам собой, когда напоминание сработает или задачу завершат, —
    ровно тот дрейф, ради устранения которого всё это и написано.

    ``request_tz`` берётся из СТРОКИ захвата, а не из обслуживаемого запроса:
    повторяющий вслепую клиент вправе прислать другой пояс (телефон переехал),
    и отражённый входной пояс назвал бы зону, которая хранимое напоминание не
    порождала.
    """
    target = await PostgresSemanticIndexWriter(session).read_initial_target(
        access_context, capture.id
    )
    if target is None or capture.request_tz is None:
        # Недостижимо: на API-пути запись создаётся всегда, а значит всегда
        # пишутся и прогон, и цель индексации — одной транзакцией с журналом;
        # а request_tz у строки channel='api' держит предикат формы в базе.
        _logger.error(
            "api /v1/captures found an incomplete capture trace_id=%s",
            trace.trace_id,
        )
        raise ApiError(500, ERROR_INTERNAL, trace)
    reminder_at: datetime | None = None
    if target.record_kind is SearchRecordType.TASK:
        reminder_at = await session.scalar(
            select(ReminderModel.remind_at).where(
                ReminderModel.source_task_id == target.record_id,
                ReminderModel.user_space_id == access_context.user_space_id,
            )
        )
    return CaptureResponse(
        capture_id=capture.id,
        record=CaptureRecordBody(type=target.record_kind.value, id=target.record_id),
        reminder_at=reminder_at.astimezone(UTC) if reminder_at is not None else None,
        request_tz=capture.request_tz,
    )


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


class _BodyTooLarge(BaseException):
    """Тело переросло cap — приватный сигнал из обёртки ``receive``.

    Наследуется от ``BaseException``, а не от ``Exception``, и это не стиль.
    FastAPI читает тело внутри ``try/except Exception`` и ЛЮБОЕ исключение
    оттуда превращает в ``HTTPException(400)`` (fastapi/routing.py), то есть
    обычное исключение доехало бы до клиента как «неверный запрос» вместо
    «слишком большое тело». ``BaseException`` мимо этого ``except`` проходит —
    ровно так же, как проходит ``CancelledError``, — и ловится только здесь.
    """


class V1IngressMiddleware:
    """Входной край `/v1`: cap тела и сеть под необработанные исключения.

    Одна прослойка на две работы намеренно. Это одна и та же забота — вход в
    `/v1`, — им нужна одна и та же проверка префикса пути, и обе обязаны стоять
    СНАРУЖИ обработчиков исключений: cap — чтобы отказать до чтения тела, сеть —
    чтобы поймать то, чего обработчики не разобрали. Две прослойки означали бы
    две проверки пути и два места, где префикс надо держать в согласии.

    Почему cap именно здесь, а не зависимостью роута: FastAPI читает и разбирает
    тело ДО того, как решает зависимости, то есть до проверки токена. Значит,
    отказать обязан кто-то, кто стоит раньше маршрутизации, иначе процесс уже
    сложил в память тело НЕИЗВЕСТНОГО отправителя. И одного ``Content-Length``
    мало: без него (chunked) объявлять нечего, поэтому байты считаются по
    чанкам — тот же приём, что у телеграмного webhook'а.

    Почему сеть — прослойка, а не обработчик ``Exception``: обработчик на
    ``Exception`` в Starlette по устройству ОБЩЕПРИЛОЖЕНЧЕСКИЙ (он вынимается из
    карты и становится handler'ом ``ServerErrorMiddleware``), пути внутри него
    проверять поздно. Зарегистрировать такой обработчик значило бы поменять
    байты ответа и обрыв соединения на путях ВНЕ `/v1` — в том числе на
    телеграмном webhook'е, чей провод трогать нельзя. Прослойка же стоит между
    ``ServerErrorMiddleware`` и обработчиками, поэтому видит всё неразобранное
    и при этом не касается чужих путей: не под `/v1` — сквозной вызов без
    ``try`` вовсе.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(
            f"{API_PREFIX}/"
        ):
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_body_bytes:
            await self._answer(scope, send, 413, ERROR_PAYLOAD_TOO_LARGE)
            return

        received = 0

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise _BodyTooLarge
            return message

        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except _BodyTooLarge:
            # Ответ уже пошёл — отправлять нечего, пусть падает как падало.
            if started:
                raise
            await self._answer(scope, send, 413, ERROR_PAYLOAD_TOO_LARGE)
        except Exception:
            if started:
                raise
            trace = TraceContext.new_root()
            # Детали — в лог сервера (exc_info), наружу только код и трассировка.
            # Путь пишется без query string: там бывают личные данные.
            _logger.error(
                "api unhandled error trace_id=%s path=%s",
                trace.trace_id,
                scope.get("path"),
                exc_info=True,
            )
            await error_response(500, ERROR_INTERNAL, trace)(scope, receive, send)

    async def _answer(
        self, scope: Scope, send: Send, status_code: int, code: str
    ) -> None:
        trace = TraceContext.new_root()
        _logger.warning(
            "api error status=%s code=%s trace_id=%s path=%s",
            status_code,
            code,
            trace.trace_id,
            scope.get("path"),
        )
        await error_response(status_code, code, trace)(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    """Заглушка ``receive`` для готового ответа: тело он не читает."""
    return {"type": "http.disconnect"}


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
    413: "Тело запроса больше допустимого размера.",
    422: (
        "Запрос не проходит проверку: пустой или слишком длинный текст, пустой "
        "или слишком длинный client_ref, неизвестный часовой пояс, неизвестный "
        "тип записи, слишком длинный адрес ссылки, нулевой байт (U+0000) в "
        "любом текстовом поле. Сюда же попадает слишком длинная заметка, если "
        "размер тела не был объявлен заранее."
    ),
    429: (
        "Либо слишком много неудачных попыток авторизации с этого адреса, либо "
        "исчерпан бюджет записей этого пространства в текущем окне."
    ),
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

"""Роутер /v1: вход по токену, конверт ошибок, GET /v1/me (эпик API-1, C1).

Проверяется то, ради чего роутер устроен именно так: пространство берётся ТОЛЬКО
из предъявленного токена (параметр запроса на выбор пространства не влияет и не
читается вовсе), любой провал авторизации отвечает ОДИНАКОВЫМ 401 без подсказки,
подбор токена упирается в лимит на адрес, а наружу уходит безопасный код ошибки
с идентификатором трассировки — без текста исключения, SQL и путей.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap import api_v1
from second_brain.bootstrap.api_v1 import (
    TRACE_ID_HEADER,
    ApiRuntime,
    ApiRuntimeProvider,
    AuthorizationFailureLimiter,
    _api_runtime_from_environment,
    client_address,
    register_v1_error_handlers,
    scoped_session,
)
from second_brain.bootstrap.app import create_app
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    assert_non_privileged_application_role,
    create_database_engine,
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresApiTokenRepository,
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.api_tokens import (
    ApiTokenLifecycle,
    AuthenticateApiToken,
)
from second_brain.slices.identity.application.contracts import AccessContext
from tests.bootstrap.conftest import set_required_environment
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
PEPPER = b"api-v1-pepper"
PEPPER_KEY_ID = "api-v1-key"
THROTTLE = timedelta(minutes=5)
FAILURE_WINDOW = timedelta(minutes=15)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture(autouse=True)
async def reset_api_v1_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


async def seed_space(
    schema_engine: AsyncEngine,
    *,
    telegram_user_id: int = 42,
    language: str | None = "ru",
    user_active: bool = True,
) -> AccessContext:
    user_id = uuid4()
    space_id = uuid4()
    async with create_session_factory(schema_engine)() as session:
        async with session.begin():
            session.add(
                User(
                    id=user_id,
                    role="member",
                    is_active=user_active,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add_all(
                [
                    UserSpace(
                        id=space_id,
                        owner_user_id=user_id,
                        timezone="Asia/Jerusalem",
                        language=language,
                        is_active=True,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    TelegramIdentity(
                        id=uuid4(),
                        telegram_user_id=telegram_user_id,
                        user_id=user_id,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
    return AccessContext(user_id=user_id, user_space_id=space_id)


async def issue_secret(engine: AsyncEngine, access_context: AccessContext) -> str:
    async with create_session_factory(engine)() as session:
        async with session.begin():
            issued = await ApiTokenLifecycle(
                pepper=PEPPER, pepper_key_id=PEPPER_KEY_ID
            ).issue(access_context, PostgresUpdateTransaction(session), NOW)
    return issued.secret


async def revoke_all(engine: AsyncEngine, access_context: AccessContext) -> None:
    lifecycle = ApiTokenLifecycle(pepper=PEPPER, pepper_key_id=PEPPER_KEY_ID)
    async with create_session_factory(engine)() as session:
        async with session.begin():
            transaction = PostgresUpdateTransaction(session)
            for view in await lifecycle.list_tokens(access_context, transaction):
                await lifecycle.revoke(access_context, transaction, view.id, NOW)


def api_runtime(
    engine: AsyncEngine,
    *,
    clock: FixedClock | None = None,
    failure_limit: int = 100,
    client_ip_header: str | None = None,
) -> ApiRuntime:
    fixed = clock or FixedClock()
    session_factory = create_session_factory(engine)
    return ApiRuntime(
        authenticate=AuthenticateApiToken(
            repository=PostgresApiTokenRepository(session_factory),
            clock=fixed,
            pepper=PEPPER,
            pepper_key_id=PEPPER_KEY_ID,
            last_used_throttle=THROTTLE,
        ),
        session_factory=session_factory,
        clock=fixed,
        failure_limit=failure_limit,
        failure_window=FAILURE_WINDOW,
        client_ip_header=client_ip_header,
    )


def constant_provider(runtime: ApiRuntime | None) -> ApiRuntimeProvider:
    async def provider() -> ApiRuntime | None:
        return runtime

    return provider


def api_app(runtime: ApiRuntime | None) -> FastAPI:
    return create_app(api_runtime_provider=constant_provider(runtime))


def broken_database_provider() -> ApiRuntimeProvider:
    """Сборка зависимостей, падающая ровно так, как при недоступной базе.

    Тот же вызов, что и в ``_api_runtime_from_environment``, по недостижимому
    адресу: наружу летит ошибка драйвера, а не RuntimeError.
    """

    async def provider() -> ApiRuntime | None:
        unreachable = create_database_engine(
            "postgresql+asyncpg://second_brain_app@127.0.0.1:1/second_brain"
        )
        try:
            await assert_non_privileged_application_role(unreachable)
        finally:
            await unreachable.dispose()
        raise AssertionError("unreachable database must not pass the role check")

    return provider


async def get_me(
    app: FastAPI,
    *,
    secret: str | None = None,
    authorization: str | None = None,
    path: str = "/v1/me",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = dict(headers or {})
    if authorization is not None:
        request_headers["Authorization"] = authorization
    elif secret is not None:
        request_headers["Authorization"] = f"Bearer {secret}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get(path, headers=request_headers)


# ---------------------------------------------------------------------------
# цепочка «заголовок → токен → пространство → данные»
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_answers_with_the_space_the_token_belongs_to(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await get_me(app, secret=secret)

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(access.user_id),
        "user_space_id": str(access.user_space_id),
        "language": "ru",
        "timezone": "Asia/Jerusalem",
    }


@pytest.mark.asyncio
async def test_each_token_reaches_only_its_own_space(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    first = await seed_space(schema_engine, telegram_user_id=1, language="ru")
    second = await seed_space(schema_engine, telegram_user_id=2, language="en")
    first_secret = await issue_secret(engine, first)
    second_secret = await issue_secret(engine, second)
    app = api_app(api_runtime(engine))

    first_response = await get_me(app, secret=first_secret)
    second_response = await get_me(app, secret=second_secret)

    assert first_response.json()["user_space_id"] == str(first.user_space_id)
    assert first_response.json()["language"] == "ru"
    assert second_response.json()["user_space_id"] == str(second.user_space_id)
    assert second_response.json()["language"] == "en"


@pytest.mark.asyncio
async def test_a_parameter_cannot_choose_somebody_elses_space(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Подделать пространство нечем: роутер параметры о нём НЕ читает вовсе.
    mine = await seed_space(schema_engine, telegram_user_id=1)
    stranger = await seed_space(schema_engine, telegram_user_id=2)
    secret = await issue_secret(engine, mine)
    app = api_app(api_runtime(engine))

    response = await get_me(
        app,
        secret=secret,
        path=(
            f"/v1/me?user_space_id={stranger.user_space_id}&user_id={stranger.user_id}"
        ),
    )

    assert response.status_code == 200
    assert response.json()["user_space_id"] == str(mine.user_space_id)
    assert response.json()["user_id"] == str(mine.user_id)


@pytest.mark.asyncio
async def test_scoped_session_tells_the_database_whose_space_it_is(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Ради этого сессия и открывается через общий механизм: дальше по эпику
    # содержательные запросы упираются в RLS, а не в аккуратность SELECT'ов.
    access = await seed_space(schema_engine)

    async with scoped_session(create_session_factory(engine), access) as session:
        scope = await session.scalar(
            text("SELECT current_setting('second_brain.user_space_id', true)")
        )

    assert scope == str(access.user_space_id)


# ---------------------------------------------------------------------------
# один и тот же 401 на любой провал
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_authorization_failure_answers_the_same_401(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Разное поведение здесь — подсказка, какой токен существует.
    access = await seed_space(schema_engine)
    revoked_secret = await issue_secret(engine, access)
    await revoke_all(engine, access)
    deactivated = await seed_space(schema_engine, telegram_user_id=7, user_active=False)
    deactivated_secret = await issue_secret(engine, deactivated)
    app = api_app(api_runtime(engine))

    responses = [
        await get_me(app),
        await get_me(app, authorization=""),
        await get_me(app, authorization="Basic dXNlcjpwYXNz"),
        await get_me(app, authorization="Bearer"),
        await get_me(app, authorization="Bearer "),
        await get_me(app, secret="never-issued-secret"),
        await get_me(app, secret=revoked_secret),
        await get_me(app, secret=deactivated_secret),
    ]

    assert [response.status_code for response in responses] == [401] * len(responses)
    bodies = [without_trace(response.json()) for response in responses]
    assert bodies == [{"error": {"code": "unauthorized"}}] * len(responses)
    # Ни один ответ не намекает, ЧТО именно не так, и не повторяет предъявленное.
    assert all("detail" not in response.text for response in responses)
    assert all(revoked_secret not in response.text for response in responses)


@pytest.mark.asyncio
async def test_the_presented_secret_never_comes_back_in_the_answer(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = api_app(api_runtime(engine))
    secret = "s3cret-guess-attempt"

    response = await get_me(app, secret=secret)

    assert secret not in response.text
    assert all(secret not in value for value in response.headers.values())


# ---------------------------------------------------------------------------
# конверт ошибок
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_carries_a_trace_id_in_body_and_header(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = api_app(api_runtime(engine))

    response = await get_me(app)

    payload = response.json()
    trace_id = payload["error"]["trace_id"]
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "trace_id"}
    assert response.headers[TRACE_ID_HEADER] == trace_id
    assert len(trace_id) == 32
    assert all(character in "0123456789abcdef" for character in trace_id)


@pytest.mark.asyncio
async def test_every_error_under_v1_uses_the_same_envelope(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    unknown_path = await get_me(app, secret=secret, path="/v1/nothing-here")
    unconfigured = await get_me(api_app(None), secret=secret)

    assert unknown_path.status_code == 404
    assert without_trace(unknown_path.json()) == {"error": {"code": "not_found"}}
    assert unconfigured.status_code == 503
    assert without_trace(unconfigured.json()) == {"error": {"code": "unavailable"}}


@pytest.mark.asyncio
async def test_a_broken_database_is_an_internal_error_not_a_401(
    schema_engine: AsyncEngine,
) -> None:
    # Недоступная база — не «неверный токен»: 401 здесь врал бы владельцу
    # живого токена, а провал попал бы в лимит на подбор.
    unreachable = create_database_engine(
        "postgresql+asyncpg://second_brain_app@127.0.0.1:1/second_brain"
    )
    app = api_app(api_runtime(unreachable))
    try:
        response = await get_me(app, secret="anything")
    finally:
        await unreachable.dispose()

    assert response.status_code == 500
    assert without_trace(response.json()) == {"error": {"code": "internal"}}
    assert "asyncpg" not in response.text
    assert "127.0.0.1" not in response.text


@pytest.mark.asyncio
async def test_a_broken_database_on_the_very_first_request_stays_in_the_envelope() -> (
    None
):
    # Зависимости собираются лениво, на первом запросе: если база недоступна
    # ИМЕННО тогда, сборка падает внутри запроса. Наружу всё равно должен уйти
    # наш конверт с трассировкой, а не голый 500 без неё.
    app = create_app(api_runtime_provider=broken_database_provider())

    response = await get_me(app, secret="anything")

    assert response.status_code == 500
    assert without_trace(response.json()) == {"error": {"code": "internal"}}
    assert response.headers[TRACE_ID_HEADER] == response.json()["error"]["trace_id"]
    assert "Traceback" not in response.text
    assert "asyncpg" not in response.text
    assert "127.0.0.1" not in response.text


@pytest.mark.asyncio
async def test_environment_runtime_closes_the_engine_on_any_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Движок в кэш не попадает, значит закрыть его больше некому: недоступная
    # база на первом запросе иначе оставляла бы пул за каждой попыткой.
    set_required_environment(monkeypatch)
    disposed: list[str] = []

    class RecordingEngine:
        async def dispose(self) -> None:
            disposed.append("dispose")

    async def refuse_with_driver_error(_engine: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection timed out"))

    monkeypatch.setattr(
        api_v1, "create_database_engine", lambda _url: RecordingEngine()
    )
    monkeypatch.setattr(
        api_v1, "assert_non_privileged_application_role", refuse_with_driver_error
    )

    with pytest.raises(OperationalError):
        await _api_runtime_from_environment()

    assert disposed == ["dispose"]


@pytest.mark.asyncio
async def test_validation_errors_under_v1_stay_in_the_envelope() -> None:
    # У `/v1/me` параметров нет, но `/v1` — фундамент всей поверхности: первый
    # же эндпоинт следующего слайса иначе ответит структурой FastAPI мимо
    # конверта. Проба живёт только здесь и в публичную схему не попадает.
    app = FastAPI()
    register_v1_error_handlers(app)

    @app.get("/v1/probe")
    async def probe(value: int) -> dict[str, int]:
        return {"value": value}

    @app.get("/outside")
    async def outside(value: int) -> dict[str, int]:
        return {"value": value}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        under_v1 = await client.get("/v1/probe")
        beyond_v1 = await client.get("/outside")

    assert under_v1.status_code == 422
    assert without_trace(under_v1.json()) == {"error": {"code": "invalid_request"}}
    assert "detail" not in under_v1.text
    assert under_v1.headers[TRACE_ID_HEADER] == under_v1.json()["error"]["trace_id"]
    # Вне `/v1` — поведение по умолчанию, его контракт не наш.
    assert beyond_v1.status_code == 422
    assert "detail" in beyond_v1.json()


# ---------------------------------------------------------------------------
# лимит на подбор токена
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_attempts_from_one_client_run_into_the_limit(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = api_app(api_runtime(engine, failure_limit=2))

    first = await get_me(app, secret="guess-1")
    second = await get_me(app, secret="guess-2")
    third = await get_me(app, secret="guess-3")

    assert [first.status_code, second.status_code] == [401, 401]
    assert third.status_code == 429
    assert without_trace(third.json()) == {"error": {"code": "too_many_requests"}}


@pytest.mark.asyncio
async def test_successful_calls_do_not_spend_the_failure_budget(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine, failure_limit=1))

    for _ in range(5):
        assert (await get_me(app, secret=secret)).status_code == 200

    assert (await get_me(app, secret="guess")).status_code == 401
    assert (await get_me(app, secret=secret)).status_code == 200


@pytest.mark.asyncio
async def test_an_exhausted_budget_does_not_lock_out_a_live_token(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Адрес в проде общий для всех вызывающих (uvicorn без --proxy-headers,
    # traefik из host-сети), поэтому «исчерпан бюджет → отказ всем» заперло бы
    # снаружи и владельца. Токен предъявлен — токен проверяется.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine, failure_limit=1))

    assert (await get_me(app, secret="guess")).status_code == 401

    owner = await get_me(app, secret=secret)
    stranger = await get_me(app, secret="guess-again")
    still_the_owner = await get_me(app, secret=secret)

    assert owner.status_code == 200
    assert stranger.status_code == 429
    assert without_trace(stranger.json()) == {"error": {"code": "too_many_requests"}}
    assert still_the_owner.status_code == 200


@pytest.mark.asyncio
async def test_an_exhausted_budget_stops_spending_on_further_failures(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Списывается только провал при живом бюджете: иначе непрерывный подбор
    # продлевал бы блокировку сам себе и окно никогда не отпускало бы адрес.
    clock = FixedClock()
    app = api_app(api_runtime(engine, clock=clock, failure_limit=1))

    assert (await get_me(app, secret="guess-1")).status_code == 401
    assert (await get_me(app, secret="guess-2")).status_code == 429

    clock.value = NOW + FAILURE_WINDOW + timedelta(minutes=1)

    assert (await get_me(app, secret="guess-3")).status_code == 401


@pytest.mark.asyncio
async def test_zero_limit_switches_the_throttle_off(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = api_app(api_runtime(engine, failure_limit=0))

    statuses = [
        (await get_me(app, secret=f"guess-{index}")).status_code for index in range(5)
    ]

    assert statuses == [401] * 5


def test_the_failure_window_lets_a_client_back_in() -> None:
    limiter = AuthorizationFailureLimiter(limit=2, window=timedelta(minutes=15))
    limiter.register_failure("1.1.1.1", NOW)
    limiter.register_failure("1.1.1.1", NOW)

    assert limiter.is_blocked("1.1.1.1", NOW) is True
    assert limiter.is_blocked("2.2.2.2", NOW) is False
    assert limiter.is_blocked("1.1.1.1", NOW + timedelta(minutes=16)) is False


@pytest.mark.asyncio
async def test_the_limit_follows_the_address_the_proxy_wrote(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Всё левее последней записи X-Forwarded-For прислал сам клиент: если брать
    # её, подбор обходится сменой строки в заголовке.
    app = api_app(
        api_runtime(engine, failure_limit=1, client_ip_header="X-Forwarded-For")
    )
    spoofed = {"X-Forwarded-For": "9.9.9.9, 1.1.1.1"}
    another = {"X-Forwarded-For": "9.9.9.9, 2.2.2.2"}

    first = await get_me(app, secret="guess", headers=spoofed)
    other_client = await get_me(app, secret="guess", headers=another)
    same_client = await get_me(app, secret="guess", headers=spoofed)

    assert first.status_code == 401
    assert other_client.status_code == 401
    assert same_client.status_code == 429


def test_without_a_configured_header_the_socket_address_is_used() -> None:
    request = fake_request({"x-forwarded-for": "9.9.9.9"}, client_host="10.0.0.7")

    assert client_address(request, None) == "10.0.0.7"
    assert client_address(request, "X-Forwarded-For") == "9.9.9.9"


# ---------------------------------------------------------------------------
# схема контракта
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openapi_describes_v1_and_keeps_the_service_routes_out(
    engine: AsyncEngine,
) -> None:
    transport = httpx.ASGITransport(app=api_app(api_runtime(engine)))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/openapi.json")

    schema = response.json()
    assert response.status_code == 200
    assert set(schema["paths"]) == {"/v1/me"}
    assert "/health" not in schema["paths"]
    assert "/telegram/webhook" not in schema["paths"]
    operation = schema["paths"]["/v1/me"]["get"]
    assert operation["security"]
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert set(operation["responses"]) >= {"200", "401", "429", "503"}
    assert "PEPPER" not in json.dumps(schema)


@pytest.mark.asyncio
async def test_service_routes_still_answer_next_to_v1(engine: AsyncEngine) -> None:
    transport = httpx.ASGITransport(app=api_app(api_runtime(engine)))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_environment_runtime_refuses_a_privileged_database_role(
    isolated_database: IsolatedDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Та же fail-closed проверка роли, что у webhook: дверь в интернет не смеет
    # работать owner/BYPASSRLS-ролью.
    set_required_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", isolated_database.schema_database_url)
    monkeypatch.setenv("SCHEMA_DATABASE_URL", isolated_database.database_url)

    with pytest.raises(RuntimeError, match="non-superuser"):
        await _api_runtime_from_environment()


@pytest.mark.asyncio
async def test_missing_environment_means_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert await _api_runtime_from_environment() is None


def without_trace(payload: dict[str, Any]) -> dict[str, Any]:
    error = dict(payload["error"])
    error.pop("trace_id", None)
    return {"error": error}


def fake_request(headers: dict[str, str], client_host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [
                (name.encode(), value.encode()) for name, value in headers.items()
            ],
            "client": (client_host, 51234),
        }
    )

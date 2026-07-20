"""POST /v1/captures: запись с телефона, повтор без дублей (эпик API-1, D1).

Проверяется то, ради чего слайс устроен именно так: захват из запроса НЕ идёт
телеграмным путём (и потому не съедает нажатую в боте кнопку), относительное
время разбирается поясом ЗАПРОСА, а слепой повтор с тем же ``client_ref``
возвращает ответ ПЕРВОГО вызова — тем же телом, поле в поле, включая пояс.
Отдельно проверяется входной край: тело сверх потолка отбивается ДО разбора и
ДО проверки токена, а необработанное исключение под `/v1` уходит наружу нашим
конвертом, не задевая при этом пути вне `/v1`.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.api_v1 import (
    TRACE_ID_HEADER,
    ApiRuntime,
    V1IngressMiddleware,
    register_v1_error_handlers,
)
from second_brain.bootstrap.app import create_app
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import (
    TaskCaptureInTransaction,
    build_task_capture,
)
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresApiTokenRepository,
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.api_tokens import AuthenticateApiToken
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
)
from second_brain.slices.tasks.application.contracts import (
    CompleteTaskCommand,
    CreateTypedCaptureCommand,
    SetPendingCaptureSelectionCommand,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType
from second_brain.slices.weblinks.adapters.persistence.models import RecordUrlModel
from tests.bootstrap.test_api_v1 import (
    FAILURE_WINDOW,
    NOW,
    PEPPER,
    PEPPER_KEY_ID,
    THROTTLE,
    FixedClock,
    issue_secret,
    seed_space,
)
from tests.identity.conftest import IsolatedDatabase

TRACE_ID = "d" * 32
WRITE_WINDOW = timedelta(minutes=1)
LISBON = "Europe/Lisbon"
JERUSALEM = "Asia/Jerusalem"
# Двоеточие обязательно: «завтра в 9» разбором НЕ читается как час — получается
# момент приёма плюс сутки, один и тот же в любом поясе. На таком тексте любое
# утверждение про пояс держится на микросекундах приёма, а не на поясе.
AT_NINE = "завтра в 9:00 позвонить"


@pytest_asyncio.fixture(autouse=True)
async def reset_capture_api_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )


def api_runtime(
    engine: AsyncEngine,
    *,
    clock: FixedClock | None = None,
    write_rate_limit: int = 1000,
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
        failure_limit=100,
        failure_window=FAILURE_WINDOW,
        client_ip_header=None,
        write_rate_limit=write_rate_limit,
        write_rate_window=WRITE_WINDOW,
    )


def api_app(runtime: ApiRuntime) -> FastAPI:
    async def provider() -> ApiRuntime | None:
        return runtime

    return create_app(api_runtime_provider=provider)


def body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": "купить лампочку",
        "client_ref": uuid4().hex,
        "tz": "Asia/Jerusalem",
    }
    payload.update(overrides)
    return payload


async def post_capture(
    app: FastAPI, secret: str | None, payload: dict[str, Any]
) -> httpx.Response:
    headers = {} if secret is None else {"Authorization": f"Bearer {secret}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post("/v1/captures", json=payload, headers=headers)


async def get_me(app: FastAPI, secret: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get("/v1/me", headers={"Authorization": f"Bearer {secret}"})


async def count_rows(engine: AsyncEngine, model: type[Any]) -> int:
    async with create_session_factory(engine)() as session:
        total = await session.scalar(select(func.count()).select_from(model))
        return int(total or 0)


def nine_tomorrow(tz: str) -> datetime:
    """«Завтра в 9:00» названного пояса как момент в UTC — эталон для разбора.

    Считается арифметикой самого пояса, а не тем же путём, что разбор текста, и
    поэтому смещение берётся на нужную дату: зашитые «08:00Z» и «06:00Z» верны
    только летом и краснели бы каждую осень на верном коде.
    """
    local = datetime.now(UTC).astimezone(ZoneInfo(tz)) + timedelta(days=1)
    return local.replace(hour=9, minute=0, second=0, microsecond=0).astimezone(UTC)


async def stored_texts(engine: AsyncEngine) -> list[str | None]:
    async with create_session_factory(engine)() as session:
        return list(await session.scalars(select(CaptureEventModel.raw_text)))


# ---------------------------------------------------------------------------
# запись создаётся, и телеграмный режим при этом не трогается
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_over_http_creates_a_record_without_telegram_ids(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(
        app,
        secret,
        body(links=[{"label": "лампа", "url": "https://example.test/lamp"}]),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["record"]["type"] == "note"
    assert payload["reminder_at"] is None
    assert payload["request_tz"] == "Asia/Jerusalem"
    assert await count_rows(schema_engine, NoteModel) == 1
    # Ссылки пишутся ТОЛЬКО внутри ветки «запись создана»: ловушка _is_eligible
    # оставила бы голую строку журнала и молча потеряла бы обе половины.
    assert await count_rows(schema_engine, RecordUrlModel) == 1
    async with schema_engine.begin() as connection:
        row = (
            await connection.execute(
                select(
                    CaptureEventModel.channel,
                    CaptureEventModel.bot_id,
                    CaptureEventModel.client_ref,
                    CaptureEventModel.request_tz,
                    CaptureEventModel.modality,
                )
            )
        ).one()
    assert row.channel == "api"
    assert row.bot_id is None
    assert row.client_ref is not None
    assert row.request_tz == "Asia/Jerusalem"
    assert row.modality == "text"


@pytest.mark.asyncio
async def test_api_capture_does_not_eat_the_pressed_button(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Зеркало телеграмного сторожа: если API-ветку когда-нибудь «упростят»
    # обратно в consume_for_text, кнопка будет съедена, а тип станет «идея».
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    async with create_session_factory(engine)() as session, session.begin():
        await TaskCaptureInTransaction().set_selection(
            SetPendingCaptureSelectionCommand(
                access_context=access,
                selection="idea",
                updated_at=NOW,
                trace_id=TRACE_ID,
            ),
            PostgresUpdateTransaction(session),
        )
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body())

    assert response.status_code == 201
    assert response.json()["record"]["type"] == "note"
    assert await count_rows(schema_engine, NoteModel) == 1
    # Кнопка, нажатая в боте, осталась нетронутой.
    assert await count_rows(schema_engine, PendingCaptureSelectionModel) == 1


@pytest.mark.asyncio
async def test_slash_text_over_http_is_an_ordinary_note(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(text="/дом купить молоко"))

    assert response.status_code == 201
    assert response.json()["record"]["type"] == "note"
    assert await count_rows(schema_engine, NoteModel) == 1


@pytest.mark.asyncio
async def test_explicit_type_beats_time(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(
        app, secret, body(text="завтра в 9 позвонить", type="note")
    )

    assert response.status_code == 201
    assert response.json()["record"]["type"] == "note"
    assert response.json()["reminder_at"] is None
    assert await count_rows(schema_engine, ReminderModel) == 0


@pytest.mark.asyncio
async def test_absent_type_routes_time_to_a_task(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(text="завтра в 9 позвонить"))

    assert response.status_code == 201
    assert response.json()["record"]["type"] == "task"
    assert response.json()["reminder_at"] is not None
    assert await count_rows(schema_engine, TaskModel) == 1
    assert await count_rows(schema_engine, ReminderModel) == 1


# ---------------------------------------------------------------------------
# часовой пояс запроса
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_is_parsed_in_the_request_timezone(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Тот самый тест, который отличает настоящее решение про пояс от косметики.
    # Утверждается КОНКРЕТНЫЙ момент каждого пояса, а не «они разные»: разность
    # сама по себе набегает и от долей секунды приёма, то есть держалась бы и на
    # коде, который пояс запроса выбрасывает.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    lisbon = await post_capture(app, secret, body(text=AT_NINE, tz=LISBON))
    jerusalem = await post_capture(app, secret, body(text=AT_NINE, tz=JERUSALEM))

    assert lisbon.status_code == 201
    assert jerusalem.status_code == 201
    assert datetime.fromisoformat(lisbon.json()["reminder_at"]) == nine_tomorrow(LISBON)
    assert datetime.fromisoformat(jerusalem.json()["reminder_at"]) == nine_tomorrow(
        JERUSALEM
    )


@pytest.mark.asyncio
async def test_space_timezone_still_governs_telegram(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Телеграм пояса не передаёт вовсе, поэтому у него по-прежнему работает пояс
    # ПРОСТРАНСТВА. Запрос по HTTP идёт нарочно с ДРУГИМ поясом: если телеграм
    # когда-нибудь поведут через пояс запроса, оба момента станут лиссабонскими
    # и тест покраснеет. С одинаковым поясом у обоих вызовов доказывать было бы
    # нечего.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    received_at = datetime.now(UTC)

    over_http = await post_capture(app, secret, body(text=AT_NINE, tz=LISBON))
    async with create_session_factory(engine)() as session, session.begin():
        await TaskCaptureInTransaction().capture(
            CaptureTextCommand(
                access_context=access,
                bot_id=10,
                telegram_update_id=8100,
                telegram_message_id=8101,
                raw_text=AT_NINE,
                received_at=received_at,
                trace_id=TRACE_ID,
            ),
            PostgresUpdateTransaction(session),
        )

    async with schema_engine.begin() as connection:
        instants = list(await connection.scalars(select(ReminderModel.remind_at)))
    over_http_at = datetime.fromisoformat(over_http.json()["reminder_at"])
    # Пояс пространства при заведении из телеграма — Asia/Jerusalem.
    telegram_at = nine_tomorrow(JERUSALEM)
    assert over_http_at == nine_tomorrow(LISBON)
    assert over_http_at != telegram_at
    assert {moment.astimezone(UTC) for moment in instants} == {
        over_http_at,
        telegram_at,
    }
    assert len(instants) == 2


@pytest.mark.asyncio
async def test_unknown_timezone_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(tz="Mars/Olympus"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_missing_timezone_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(
        app, secret, {"text": "купить лампочку", "client_ref": uuid4().hex}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


# ---------------------------------------------------------------------------
# повтор возвращает ответ ПЕРВОГО вызова
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeat_with_the_same_client_ref_returns_the_first_answer(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    payload = body(text="завтра в 9 позвонить")

    first = await post_capture(app, secret, payload)
    repeat = await post_capture(app, secret, payload)

    assert first.status_code == 201
    assert repeat.status_code == 200
    assert first.json() == repeat.json()
    assert await count_rows(schema_engine, CaptureEventModel) == 1
    assert await count_rows(schema_engine, TaskModel) == 1


@pytest.mark.asyncio
async def test_a_repeat_that_changes_text_and_tz_still_returns_the_first_answer(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Реализация, отражающая пояс ИЗ ЗАПРОСА, проходит все остальные тесты файла
    # и падает только здесь — поэтому сверяется всё тело, а пояс реально меняется.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    client_ref = uuid4().hex

    first = await post_capture(
        app,
        secret,
        body(text="завтра в 9 позвонить", tz="Europe/Lisbon", client_ref=client_ref),
    )
    repeat = await post_capture(
        app,
        secret,
        body(text="совсем другой текст", tz="Asia/Jerusalem", client_ref=client_ref),
    )

    assert first.status_code == 201
    assert repeat.status_code == 200
    assert first.json() == repeat.json()
    assert repeat.json()["request_tz"] == "Europe/Lisbon"


@pytest.mark.asyncio
async def test_repeat_after_the_classifier_added_records_still_returns_the_first_record(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Тот самый тест, который краснеет против отвергнутой схемы «самая ранняя
    # запись по created_at»: разбор текста дописывает свои записи к тому же
    # захвату, и такая схема вернула бы одну из них.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    payload = body()

    first = await post_capture(app, secret, payload)
    capture_id = UUID(first.json()["capture_id"])
    async with create_session_factory(engine)() as session, session.begin():
        await build_task_capture(session).create_for_selection(
            CreateTypedCaptureCommand(
                access_context=access,
                selection=PendingCaptureType.IDEA,
                text="добавлено разбором",
                source_capture_event_id=capture_id,
                created_at=NOW - timedelta(days=1),
                trace_id=TRACE_ID,
            )
        )
    repeat = await post_capture(app, secret, payload)

    assert repeat.status_code == 200
    assert repeat.json() == first.json()
    assert repeat.json()["record"]["type"] == "note"


@pytest.mark.asyncio
async def test_repeat_after_the_reminder_was_cancelled_still_returns_the_instant(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Повтор — пере-выдача первого ответа, а не свежая сводка: фильтр по статусу
    # заставил бы ответ измениться сам собой после завершения задачи.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    payload = body(text="завтра в 9 позвонить")

    first = await post_capture(app, secret, payload)
    async with schema_engine.begin() as connection:
        task_id = await connection.scalar(select(TaskModel.id))
    assert task_id is not None
    async with create_session_factory(engine)() as session, session.begin():
        await TaskCaptureInTransaction().complete(
            CompleteTaskCommand(
                access_context=access,
                task_id=task_id,
                completed_at=NOW,
                trace_id=TRACE_ID,
            ),
            PostgresUpdateTransaction(session),
        )
    repeat = await post_capture(app, secret, payload)

    assert repeat.status_code == 200
    assert repeat.json() == first.json()
    assert repeat.json()["reminder_at"] is not None


@pytest.mark.asyncio
async def test_repeat_with_different_text_returns_the_first_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    client_ref = uuid4().hex

    first = await post_capture(
        app, secret, body(text="первый текст", client_ref=client_ref)
    )
    repeat = await post_capture(
        app, secret, body(text="второй текст", client_ref=client_ref)
    )

    assert repeat.status_code == 200
    assert repeat.json()["capture_id"] == first.json()["capture_id"]
    assert await stored_texts(schema_engine) == ["первый текст"]


@pytest.mark.asyncio
async def test_client_ref_is_scoped_to_the_space(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    mine = await seed_space(schema_engine, telegram_user_id=1)
    stranger = await seed_space(schema_engine, telegram_user_id=2)
    my_secret = await issue_secret(engine, mine)
    stranger_secret = await issue_secret(engine, stranger)
    app = api_app(api_runtime(engine))
    client_ref = uuid4().hex

    first = await post_capture(app, my_secret, body(client_ref=client_ref))
    second = await post_capture(app, stranger_secret, body(client_ref=client_ref))

    assert [first.status_code, second.status_code] == [201, 201]
    assert first.json()["capture_id"] != second.json()["capture_id"]
    assert await count_rows(schema_engine, CaptureEventModel) == 2


@pytest.mark.asyncio
async def test_api_capture_cannot_reach_another_space(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Пространство роутер не читает ни из тела, ни из пути: подделать нечего,
    # поэтому доказательство — куда легла строка и что чужие не тронуты.
    mine = await seed_space(schema_engine, telegram_user_id=1)
    stranger = await seed_space(schema_engine, telegram_user_id=2)
    secret = await issue_secret(engine, mine)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body())

    assert response.status_code == 201
    async with schema_engine.begin() as connection:
        spaces = list(await connection.scalars(select(CaptureEventModel.user_space_id)))
    assert spaces == [mine.user_space_id]
    assert stranger.user_space_id not in spaces


# ---------------------------------------------------------------------------
# что контракт не принимает
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_text_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Без min_length это был бы 500 от ck_capture_events_kind_content, а
    # единственная верная реакция клиента на 500 — повторять вечно.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(text=""))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_whitespace_only_text_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(text="   \n "))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_preserved(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Обрезка решает ТОЛЬКО «принимать ли»: журнал держит присланное дословно.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(text=" купить лампочку "))

    assert response.status_code == 201
    assert await stored_texts(schema_engine) == [" купить лампочку "]


@pytest.mark.asyncio
async def test_empty_client_ref_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Пустая строка — прекрасный уникальный ключ: без этой проверки ПЕРВЫЙ такой
    # захват возвращался бы всем последующим как «успешно записано».
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(client_ref=""))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_overlong_client_ref_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Иначе строка не влезла бы в кортеж btree-индекса и умерла бы на INSERT'е —
    # то есть 500 на каждый повтор этого запроса, навсегда.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(client_ref="x" * 3000))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(body(text="купить\x00лампочку"), id="text"),
        pytest.param(body(client_ref="ref\x001"), id="client_ref"),
        pytest.param(
            body(links=[{"label": "ла\x00мпа", "url": "https://example.test/lamp"}]),
            id="label",
        ),
        pytest.param(
            body(links=[{"label": "лампа", "url": "https://example.test/l\x00amp"}]),
            id="url",
        ),
    ],
)
@pytest.mark.asyncio
async def test_nul_byte_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine, payload: dict[str, Any]
) -> None:
    # U+0000 контракт принимал, а PostgreSQL в text/varchar не берёт вовсе:
    # захват откатывался бы уже на INSERT'е. Для очереди клиента 500 значит
    # «повтори позже» — то есть такой захват не прошёл бы НИКОГДА и намертво
    # запер бы за собой всю очередь. Отказывать надо здесь и 422.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


def incompressible_path(chars: int) -> str:
    """Кусок пути из РАЗНЫХ многобайтовых букв — то, что индекс не сожмёт.

    Однообразный путь той же длины проверял бы не то: перед укладкой в кортеж
    индекс значение жмёт, поэтому «яяяя…» и в 3 КБ проходит. Ловушка ловится
    только несжимаемым — на нём потолок кортежа и упирается по-настоящему.
    Набор букв фиксирован seed'ом, чтобы тест не мигал.
    """
    alphabet = [chr(code) for code in range(0x0400, 0x04FF)]
    return "".join(Random(7).choices(alphabet, k=chars))


@pytest.mark.asyncio
async def test_url_too_long_for_the_title_index_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Тот же капкан, что у client_ref, но по БАЙТАМ: потолок кортежа btree
    # байтовый (~2704), а потолок поля — знаковый. Кириллический путь влезает и
    # в тело, и в 2048 знаков, а normalized_url из него в
    # uq_page_titles_space_normalized_url уже нет — то есть вечный 500 на
    # повторе. До проверки этот запрос отвечал именно 500.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    url = "https://example.test/" + incompressible_path(1_400)

    response = await post_capture(
        app, secret, body(links=[{"label": "лампа", "url": url}])
    )

    assert len(url) < 2_048
    assert len(url.encode()) > 2_704
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_a_url_whose_host_only_grows_in_normalization_is_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Сторож ВЫБОРА места проверки, а не отдельного падения: сырой адрес здесь в
    # потолок влезает, а нормализованный — нет, потому что IDNA раздувает
    # не-ASCII хост в разы (é → xn--9ca, 2 байта → 7). Реализация, мерящая байты
    # ПРИСЛАННОГО, проходит соседний тест и краснеет только здесь.
    #
    # 500 этот конкретный адрес не давал: он однообразный и потому сжимается.
    # Тем он и полезен — на потолок по НЕсжатой длине опереться можно, на
    # «обычно влезает» нельзя.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))
    url = "https://" + ".".join(["é"] * 400) + "/"

    response = await post_capture(
        app, secret, body(links=[{"label": "лампа", "url": url}])
    )

    assert len(url.encode()) < 2_048
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_a_long_link_label_is_accepted(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Слово ссылки лежит в Text без потолка и никуда в индекс не идёт: границу
    # ему ставит только cap тела. Отдельный потолок отказывал бы в законном.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(
        app,
        secret,
        body(links=[{"label": "л" * 2_000, "url": "https://example.test/lamp"}]),
    )

    assert response.status_code == 201
    assert await count_rows(schema_engine, RecordUrlModel) == 1


# ---------------------------------------------------------------------------
# входной край: cap тела и сеть под исключения
# ---------------------------------------------------------------------------


def probe_app() -> FastAPI:
    """Приложение-проба: два роута, которые падают, по разные стороны `/v1`."""
    app = FastAPI()
    register_v1_error_handlers(app)

    @app.get("/v1/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("свидетельство с внутренностями")

    @app.get("/outside-boom")
    async def outside_boom() -> dict[str, str]:
        raise RuntimeError("свидетельство с внутренностями")

    app.add_middleware(V1IngressMiddleware, max_body_bytes=1024)
    return app


@pytest.mark.asyncio
async def test_unhandled_error_still_answers_with_the_envelope() -> None:
    transport = httpx.ASGITransport(app=probe_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/v1/boom")

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "internal"
    assert response.headers[TRACE_ID_HEADER] == payload["error"]["trace_id"]
    assert "свидетельство" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_a_non_v1_exception_is_not_caught_by_the_v1_net() -> None:
    # Краснеет в ту же секунду, как кто-нибудь зарегистрирует обработчик на
    # Exception: он общеприложенческий и меняет провод webhook'а.
    #
    # Работу здесь делает карта обработчиков НАСТОЯЩЕГО приложения, а не
    # подъём исключения ниже: ServerErrorMiddleware у Starlette перевозбуждает
    # исключение в любом случае, поэтому pytest.raises зелен и с обработчиком на
    # Exception, и без него. Ключ 500 проверяется вместе с классом: Starlette
    # вынимает из карты оба и оба делают одно и то же.
    app = create_app()
    assert Exception not in app.exception_handlers
    assert 500 not in app.exception_handlers

    transport = httpx.ASGITransport(app=probe_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        with pytest.raises(RuntimeError):
            await client.get("/outside-boom")


@pytest.mark.asyncio
async def test_body_over_the_cap_is_refused(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    response = await post_capture(app, secret, body(text="я" * 40_000))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.headers[TRACE_ID_HEADER]
    assert await count_rows(schema_engine, CaptureEventModel) == 0


@pytest.mark.asyncio
async def test_an_oversized_body_without_content_length_is_refused(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Обе половины и есть тест: обычный httpx всегда ставит Content-Length, а
    # без токена проверяется, что cap держит РАНЬШЕ проверки токена — FastAPI
    # разбирает тело до решения зависимостей.
    access = await seed_space(schema_engine)
    await issue_secret(engine, access)
    app = api_app(api_runtime(engine))

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"text": "'
        for _ in range(40):
            yield b"a" * 4096
        yield b'", "client_ref": "x", "tz": "Asia/Jerusalem"}'

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        request = client.build_request("POST", "/v1/captures", content=chunks())
        assert "content-length" not in request.headers
        response = await client.send(request)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert await count_rows(schema_engine, CaptureEventModel) == 0


# ---------------------------------------------------------------------------
# бюджет записей
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_budget_returns_429_but_reads_still_pass(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine, write_rate_limit=1))

    first = await post_capture(app, secret, body())
    second = await post_capture(app, secret, body())
    read = await get_me(app, secret)

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "too_many_requests"
    # Лимит стоит на записи, а не на токене.
    assert read.status_code == 200


@pytest.mark.asyncio
async def test_write_budget_counts_rejected_bodies(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Бюджет тратит попытка, а не успех: иначе зациклившееся приложение
    # разбирало бы кривые тела бесплатно.
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine, write_rate_limit=2))

    rejected = [
        (await post_capture(app, secret, body(text=""))).status_code for _ in range(2)
    ]
    after = await post_capture(app, secret, body())

    assert rejected == [422, 422]
    assert after.status_code == 429


@pytest.mark.asyncio
async def test_write_budget_is_off_when_zero(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    access = await seed_space(schema_engine)
    secret = await issue_secret(engine, access)
    app = api_app(api_runtime(engine, write_rate_limit=0))

    statuses = [(await post_capture(app, secret, body())).status_code for _ in range(5)]

    assert statuses == [201] * 5

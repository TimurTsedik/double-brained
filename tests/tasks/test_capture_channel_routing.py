"""Телеграмный путь захвата не сдвинулся ни на шаг (эпик API-1, D1).

Развилка по каналу появилась внутри той самой функции, через которую пишет бот,
поэтому у неё нужен сторож с телеграмной стороны: нажатая кнопка обязана
по-прежнему ПОТРЕБЛЯТЬСЯ (строка режима удаляется) и обязана решать тип записи.
Если телеграм когда-нибудь уедет в ветку ``create_for_selection``, кнопка
останется нетронутой, а тип свалится к умолчанию — тест краснеет обоими
утверждениями сразу.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    IdeaModel,
    NoteModel,
)
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
)
from second_brain.slices.tasks.application.contracts import (
    SetPendingCaptureSelectionCommand,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
TRACE_ID = "c" * 32
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_routing_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                {
                    "id": ACCESS.user_id,
                    "role": "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": ACCESS.user_space_id,
                    "owner_user_id": ACCESS.user_id,
                    "timezone": "Asia/Jerusalem",
                    "language": "ru",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ],
        )


async def count_rows(engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(engine)() as session:
        total = await session.scalar(select(func.count()).select_from(model))
        return int(total or 0)


@pytest.mark.asyncio
async def test_telegram_capture_still_consumes_the_pressed_button(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = TaskCaptureInTransaction()
    async with create_session_factory(engine)() as session, session.begin():
        transaction = PostgresUpdateTransaction(session)
        await capture.set_selection(
            SetPendingCaptureSelectionCommand(
                access_context=ACCESS,
                selection="idea",
                updated_at=NOW,
                trace_id=TRACE_ID,
            ),
            transaction,
        )
        await capture.capture(
            CaptureTextCommand(
                access_context=ACCESS,
                bot_id=10,
                telegram_update_id=9100,
                telegram_message_id=9101,
                raw_text="снять кино про лампочку",
                received_at=NOW,
                trace_id=TRACE_ID,
            ),
            transaction,
        )

    assert await count_rows(schema_engine, IdeaModel) == 1
    assert await count_rows(schema_engine, NoteModel) == 0
    # Кнопка потреблена: строка режима удалена, следующий текст пойдёт дефолтом.
    assert await count_rows(schema_engine, PendingCaptureSelectionModel) == 0


@pytest.mark.asyncio
async def test_telegram_text_starting_with_a_slash_stays_a_command(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Вторая половина того же сторожа: по телеграму «/…» — команда бота, записи
    # из неё не рождается. По HTTP это обычный текст (см. тесты эндпоинта).
    capture = TaskCaptureInTransaction()
    async with create_session_factory(engine)() as session, session.begin():
        await capture.capture(
            CaptureTextCommand(
                access_context=ACCESS,
                bot_id=10,
                telegram_update_id=9200,
                telegram_message_id=9201,
                raw_text="/дом купить молоко",
                received_at=NOW,
                trace_id=TRACE_ID,
            ),
            PostgresUpdateTransaction(session),
        )

    assert await count_rows(schema_engine, NoteModel) == 0
    assert await count_rows(schema_engine, IdeaModel) == 0

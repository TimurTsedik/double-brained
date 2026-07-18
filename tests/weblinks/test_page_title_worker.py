"""Шаг воркера page_titles: claim → фетч ВНЕ транзакции → итог.

Модель попыток — по образцу reminder-delivery: attempt_count++, бэкофф в
next_attempt_at, после потолка — failed навсегда. TITLE_FETCH_ENABLED=off →
шаг не клеймит вовсе.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.page_title_fetch import PageTitleFetchStep
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.weblinks.adapters.persistence.models import PageTitleModel
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresWeblinkWriter,
)
from second_brain.slices.weblinks.application.contracts import (
    RecordUrlEntry,
    SaveRecordLinksCommand,
)
from second_brain.slices.weblinks.domain.entities import (
    PageTitleStatus,
    WeblinkRecordKind,
)
from second_brain.slices.weblinks.ports.title_fetcher import TitleFetchOutcome
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
BACKOFF = timedelta(seconds=60)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


class ScriptedFetcher:
    """Фейковый TitleFetcher: очередь исходов + журнал URL."""

    def __init__(self, outcomes: list[TitleFetchOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    async def fetch_title(self, url: str) -> TitleFetchOutcome:
        self.calls.append(url)
        if not self._outcomes:
            raise AssertionError("fetcher called more times than scripted")
        return self._outcomes.pop(0)


@pytest_asyncio.fixture(autouse=True)
async def reset_page_title_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="member",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=ACCESS.user_space_id,
                owner_user_id=ACCESS.user_id,
                timezone="Asia/Jerusalem",
                language="ru",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


async def enqueue(engine: AsyncEngine, url: str, created_at: datetime = NOW) -> None:
    async with create_session_factory(engine)() as session, session.begin():
        await PostgresWeblinkWriter(session).save_links(
            SaveRecordLinksCommand(
                access_context=ACCESS,
                record_kind=WeblinkRecordKind.NOTE,
                record_id=uuid4(),
                entries=(RecordUrlEntry(label=url, url=url),),
                created_at=created_at,
                trace_id="1" * 32,
            )
        )


async def read_titles(schema_engine: AsyncEngine) -> list[PageTitleModel]:
    async with create_session_factory(schema_engine)() as session:
        return list(
            (
                await session.scalars(
                    select(PageTitleModel).order_by(PageTitleModel.original_url)
                )
            ).all()
        )


def step(
    engine: AsyncEngine,
    fetcher: ScriptedFetcher,
    *,
    enabled: bool = True,
    max_attempts: int = 3,
) -> PageTitleFetchStep:
    return PageTitleFetchStep(
        create_session_factory(engine),
        fetcher,
        enabled=enabled,
        max_attempts=max_attempts,
        retry_backoff=BACKOFF,
    )


@pytest.mark.asyncio
async def test_happy_path_marks_the_row_fetched_with_the_title(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await enqueue(engine, "https://a.example/one")
    fetcher = ScriptedFetcher([TitleFetchOutcome(ok=True, title="Заголовок")])

    worked = await step(engine, fetcher).process_once(ACCESS, NOW)

    assert worked
    assert fetcher.calls == ["https://a.example/one"]
    (row,) = await read_titles(schema_engine)
    assert row.status is PageTitleStatus.FETCHED
    assert row.title == "Заголовок"
    assert row.fetched_at == NOW
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_fetched_page_without_title_is_final_and_not_retried(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await enqueue(engine, "https://a.example/no-title")
    fetcher = ScriptedFetcher([TitleFetchOutcome(ok=True, title=None)])

    await step(engine, fetcher).process_once(ACCESS, NOW)
    # Второй тик: строка больше не pending — фетчер не дёргается.
    assert not await step(engine, ScriptedFetcher([])).process_once(ACCESS, NOW)

    (row,) = await read_titles(schema_engine)
    assert row.status is PageTitleStatus.FETCHED
    assert row.title is None


@pytest.mark.asyncio
async def test_failure_backs_off_then_reaches_the_failed_ceiling(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await enqueue(engine, "https://a.example/broken")
    failure = TitleFetchOutcome(ok=False)

    # Попытка 1: сбой → строка pending, бэкофф вперёд, в этот же тик не берётся.
    assert await step(engine, ScriptedFetcher([failure])).process_once(ACCESS, NOW)
    (row,) = await read_titles(schema_engine)
    assert row.status is PageTitleStatus.PENDING
    assert row.attempt_count == 1
    assert row.next_attempt_at == NOW + BACKOFF

    # До созревания бэкоффа строка не клеймится.
    assert not await step(engine, ScriptedFetcher([])).process_once(ACCESS, NOW)

    # Попытки 2 и 3 (потолок max_attempts=3) — после созревания каждого бэкоффа.
    second_tick = NOW + BACKOFF
    assert await step(engine, ScriptedFetcher([failure])).process_once(
        ACCESS, second_tick
    )
    (row,) = await read_titles(schema_engine)
    assert row.status is PageTitleStatus.PENDING
    assert row.attempt_count == 2
    assert row.next_attempt_at == second_tick + 2 * BACKOFF

    third_tick = second_tick + 2 * BACKOFF
    assert await step(engine, ScriptedFetcher([failure])).process_once(
        ACCESS, third_tick
    )
    (row,) = await read_titles(schema_engine)
    assert row.status is PageTitleStatus.FAILED
    assert row.attempt_count == 3
    assert row.title is None

    # failed никогда больше не клеймится.
    far_future = third_tick + 100 * BACKOFF
    assert not await step(engine, ScriptedFetcher([])).process_once(ACCESS, far_future)


@pytest.mark.asyncio
async def test_disabled_step_claims_nothing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await enqueue(engine, "https://a.example/off")
    fetcher = ScriptedFetcher([])

    worked = await step(engine, fetcher, enabled=False).process_once(ACCESS, NOW)

    assert not worked
    assert fetcher.calls == []
    (row,) = await read_titles(schema_engine)
    assert row.status is PageTitleStatus.PENDING
    assert row.attempt_count == 0


@pytest.mark.asyncio
async def test_one_tick_drains_all_due_rows_one_claim_per_transaction(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Разные created_at: клейм упорядочен (created_at, id), равные метки дали
    # бы UUID-лотерею порядка — вечный урок про тай-брейки в тестах. Первая
    # строка РАНЬШЕ NOW (обе должны быть созревшими к моменту тика).
    await enqueue(engine, "https://a.example/1", created_at=NOW - timedelta(seconds=1))
    await enqueue(engine, "https://a.example/2")
    fetcher = ScriptedFetcher(
        [
            TitleFetchOutcome(ok=True, title="Один"),
            TitleFetchOutcome(ok=True, title="Два"),
        ]
    )

    worked = await step(engine, fetcher).process_once(ACCESS, NOW)

    assert worked
    rows = await read_titles(schema_engine)
    assert [row.title for row in rows] == ["Один", "Два"]
    assert all(row.status is PageTitleStatus.FETCHED for row in rows)

"""Сводка за период (retrieval-слой): границы календаря, счётчики, страница.

Начало периода считается ЛОКАЛЬНЫМ календарём пространства (ZoneInfo, понедельник
фиксированно) и лишь затем конвертируется в UTC — никакой арифметики «минус N
дней». Счётчики и страница фильтруют ОДИН снимок `period_start <= created_at <=
as_of`; порядок детерминированный: created_at DESC, затем тип и id.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    IdeaModel,
    NoteModel,
    QuestionModel,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresDigestReader,
)
from second_brain.slices.retrieval.application.digest import (
    DIGEST_PAGE_SIZE,
    BuildDigest,
    digest_period_start,
)
from second_brain.slices.retrieval.domain.entities import (
    DigestCounters,
    DigestPeriod,
    SearchRecordType,
)
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.tasks.domain.entities import TaskStatus
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.test_semantic_index_persistence import (
    ACCESS_A,
    ACCESS_B,
    TRACE_ID,
    add_capture,
    add_reminder,
    space_row,
    user_row,
)

TZ = ZoneInfo("Asia/Jerusalem")
# Среда 15.07.2026: календарная неделя пространства идёт с понедельника
# 13.07.2026 00:00+03:00 (= 12.07.2026 21:00 UTC).
AS_OF = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WEEK_START_UTC = datetime(2026, 7, 12, 21, 0, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def reset_digest_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [user_row(ACCESS_A), user_row(ACCESS_B)])
        await connection.execute(
            insert(UserSpace), [space_row(ACCESS_A), space_row(ACCESS_B)]
        )


_KNOWLEDGE_MODELS = {
    SearchRecordType.NOTE: NoteModel,
    SearchRecordType.IDEA: IdeaModel,
    SearchRecordType.DECISION: DecisionModel,
    SearchRecordType.QUESTION: QuestionModel,
}


async def add_record(
    schema_engine: AsyncEngine,
    access: AccessContext,
    capture_event_id: UUID,
    record_type: SearchRecordType,
    text: str,
    created_at: datetime,
    record_id: UUID | None = None,
    status: TaskStatus = TaskStatus.INBOX,
) -> UUID:
    record_id = record_id or uuid4()
    async with schema_engine.begin() as connection:
        if record_type is SearchRecordType.TASK:
            await connection.execute(
                insert(TaskModel).values(
                    id=record_id,
                    user_space_id=access.user_space_id,
                    title=text,
                    description=None,
                    status=status,
                    source_capture_event_id=capture_event_id,
                    created_at=created_at,
                    updated_at=created_at,
                    trace_id=TRACE_ID,
                )
            )
        else:
            await connection.execute(
                insert(_KNOWLEDGE_MODELS[record_type]).values(
                    id=record_id,
                    user_space_id=access.user_space_id,
                    text=text,
                    source_capture_event_id=capture_event_id,
                    created_at=created_at,
                    updated_at=created_at,
                    trace_id=TRACE_ID,
                )
            )
    return record_id


async def read_digest(
    engine: AsyncEngine,
    access: AccessContext,
    period: DigestPeriod,
    offset: int,
    as_of: datetime,
):
    async with create_session_factory(engine)() as session:
        async with session.begin():
            return await BuildDigest(PostgresDigestReader(session)).read_page(
                access, period, offset, as_of, TZ
            )


# ---------------------------------------------------------------------------
# чистые границы календаря (ZoneInfo пространства)
# ---------------------------------------------------------------------------


def test_week_starts_on_monday_midnight_of_the_space_calendar() -> None:
    sunday_late = datetime(2026, 7, 12, 23, 59, tzinfo=TZ)
    monday_early = datetime(2026, 7, 13, 0, 1, tzinfo=TZ)

    before = digest_period_start(DigestPeriod.WEEK, sunday_late.astimezone(UTC), TZ)
    after = digest_period_start(DigestPeriod.WEEK, monday_early.astimezone(UTC), TZ)

    assert before == datetime(2026, 7, 6, tzinfo=TZ)
    assert after == datetime(2026, 7, 13, tzinfo=TZ)
    assert after.astimezone(UTC) == WEEK_START_UTC


def test_month_half_year_and_year_boundaries_in_the_space_calendar() -> None:
    june_last = datetime(2026, 6, 30, 23, 59, tzinfo=TZ).astimezone(UTC)
    july_first = datetime(2026, 7, 1, 0, 0, tzinfo=TZ).astimezone(UTC)
    year_last = datetime(2025, 12, 31, 23, 59, tzinfo=TZ).astimezone(UTC)

    assert digest_period_start(DigestPeriod.MONTH, june_last, TZ) == (
        datetime(2026, 6, 1, tzinfo=TZ)
    )
    assert digest_period_start(DigestPeriod.MONTH, july_first, TZ) == (
        datetime(2026, 7, 1, tzinfo=TZ)
    )
    assert digest_period_start(DigestPeriod.HALF_YEAR, june_last, TZ) == (
        datetime(2026, 1, 1, tzinfo=TZ)
    )
    assert digest_period_start(DigestPeriod.HALF_YEAR, july_first, TZ) == (
        datetime(2026, 7, 1, tzinfo=TZ)
    )
    assert digest_period_start(DigestPeriod.YEAR, year_last, TZ) == (
        datetime(2025, 1, 1, tzinfo=TZ)
    )
    assert digest_period_start(DigestPeriod.YEAR, july_first, TZ) == (
        datetime(2026, 1, 1, tzinfo=TZ)
    )


def test_period_start_crosses_the_dst_shift_of_the_space_timezone() -> None:
    # Израиль-2026: переход на летнее время 27.03 (+02:00 → +03:00). Начало
    # недели/месяца — ЛОКАЛЬНАЯ полночь со СВОИМ смещением, не «as_of минус N».
    after_shift = datetime(2026, 3, 29, 12, 0, tzinfo=TZ)
    assert after_shift.utcoffset() == timedelta(hours=3)

    week_start = digest_period_start(DigestPeriod.WEEK, after_shift.astimezone(UTC), TZ)
    month_start = digest_period_start(
        DigestPeriod.MONTH, after_shift.astimezone(UTC), TZ
    )
    april_start = digest_period_start(
        DigestPeriod.MONTH, datetime(2026, 4, 2, 12, 0, tzinfo=TZ).astimezone(UTC), TZ
    )

    assert week_start.astimezone(UTC) == datetime(2026, 3, 22, 22, 0, tzinfo=UTC)
    assert week_start.utcoffset() == timedelta(hours=2)
    assert month_start.astimezone(UTC) == datetime(2026, 2, 28, 22, 0, tzinfo=UTC)
    assert april_start.astimezone(UTC) == datetime(2026, 3, 31, 21, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# счётчики и страница на живом PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counters_match_the_listed_records_including_completed_tasks(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    in_week = AS_OF - timedelta(hours=1)
    await add_record(
        schema_engine, ACCESS_A, capture, SearchRecordType.NOTE, "заметка 1", in_week
    )
    await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "заметка 2",
        in_week - timedelta(minutes=1),
    )
    await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.TASK,
        "открытая задача",
        in_week - timedelta(minutes=2),
    )
    done_id = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.TASK,
        "сделанная задача",
        in_week - timedelta(minutes=3),
        status=TaskStatus.COMPLETED,
    )
    await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.IDEA,
        "идея",
        in_week - timedelta(minutes=4),
    )
    await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.DECISION,
        "решение",
        in_week - timedelta(minutes=5),
    )
    # Вне недели — не попадает ни в счётчики, ни в список.
    await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "прошлая неделя",
        WEEK_START_UTC - timedelta(hours=1),
    )

    page = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)

    assert page.counters == DigestCounters(
        notes=2, tasks=2, tasks_completed=1, ideas=1, decisions=1, questions=0
    )
    assert page.total == 6
    assert len(page.items) == 6
    # Новые сверху; завершённая задача несёт флаг для метки ☑️.
    assert [item.text for item in page.items] == [
        "заметка 1",
        "заметка 2",
        "открытая задача",
        "сделанная задача",
        "идея",
        "решение",
    ]
    done = next(item for item in page.items if item.id == done_id)
    assert done.task_completed is True
    # Даты страницы — в поясе пространства (июль: +03:00).
    assert all(item.created_at.utcoffset() == timedelta(hours=3) for item in page.items)
    assert page.period_start == datetime(2026, 7, 13, tzinfo=TZ)
    assert page.as_of == AS_OF.astimezone(TZ)


@pytest.mark.asyncio
async def test_completed_alarm_tasks_are_hidden_from_counters_and_list(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    in_week = AS_OF - timedelta(hours=1)
    # Завершённая задача-будильник — шум, скрыта и из счётчиков, и из списка.
    alarm_done = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.TASK,
        "будильник сделан",
        in_week,
        status=TaskStatus.COMPLETED,
    )
    await add_reminder(schema_engine, ACCESS_A, alarm_done)
    # Завершённая БЕЗ напоминания — остаётся: и в счётчике ☑️, и в списке.
    plain_done = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.TASK,
        "сделана без напоминания",
        in_week - timedelta(minutes=1),
        status=TaskStatus.COMPLETED,
    )
    # Незавершённая С напоминанием — остаётся видимой.
    alarm_pending = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.TASK,
        "будильник в работе",
        in_week - timedelta(minutes=2),
    )
    await add_reminder(schema_engine, ACCESS_A, alarm_pending)
    note_id = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "заметка рядом",
        in_week - timedelta(minutes=3),
    )

    page = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)

    assert page.counters == DigestCounters(
        notes=1, tasks=2, tasks_completed=1, ideas=0, decisions=0, questions=0
    )
    listed = {item.id for item in page.items}
    assert alarm_done not in listed
    assert listed == {plain_done, alarm_pending, note_id}
    # Счётчики и список — один снимок: total сходится с фактической страницей.
    assert page.total == len(page.items) == 3


@pytest.mark.asyncio
async def test_page_is_bounded_by_the_calendar_start_and_the_as_of_snapshot(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    sunday_late = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "воскресенье 23:59",
        WEEK_START_UTC - timedelta(minutes=1),
    )
    at_start = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "ровно понедельник",
        WEEK_START_UTC,
    )
    at_as_of = await add_record(
        schema_engine, ACCESS_A, capture, SearchRecordType.NOTE, "ровно as_of", AS_OF
    )
    after_as_of = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "создана после снимка",
        AS_OF + timedelta(seconds=1),
    )

    page = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)

    listed = {item.id for item in page.items}
    assert listed == {at_start, at_as_of}
    assert sunday_late not in listed
    # Запись после as_of не видна ни списку, ни счётчикам — снимок стабилен.
    assert after_as_of not in listed
    assert page.counters.notes == 2
    assert page.total == 2


@pytest.mark.asyncio
async def test_equal_timestamps_are_ordered_by_type_then_id(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    moment = AS_OF - timedelta(hours=2)
    newest = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.QUESTION,
        "новее всех",
        moment + timedelta(minutes=1),
    )
    note_id = await add_record(
        schema_engine, ACCESS_A, capture, SearchRecordType.NOTE, "нота", moment
    )
    idea_id = await add_record(
        schema_engine, ACCESS_A, capture, SearchRecordType.IDEA, "идея", moment
    )
    decision_id = await add_record(
        schema_engine, ACCESS_A, capture, SearchRecordType.DECISION, "решение", moment
    )
    low_note = await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "нота с меньшим id",
        moment,
        record_id=UUID(int=1),
    )

    page = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)

    # created_at DESC, при равенстве — тип (алфавит значений), затем id.
    assert [item.id for item in page.items] == [
        newest,
        decision_id,
        idea_id,
        low_note,
        note_id,
    ]


@pytest.mark.asyncio
async def test_pagination_slices_the_same_snapshot_into_10_10_5(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    for number in range(25):
        await add_record(
            schema_engine,
            ACCESS_A,
            capture,
            SearchRecordType.NOTE,
            f"note {number:02d}",
            AS_OF - timedelta(minutes=number),
        )

    first = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)
    second = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 10, AS_OF)
    third = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 20, AS_OF)
    past_end = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 25, AS_OF)

    assert DIGEST_PAGE_SIZE == 10
    assert (first.total, second.total, third.total) == (25, 25, 25)
    assert [len(page.items) for page in (first, second, third)] == [10, 10, 5]
    texts = [item.text for page in (first, second, third) for item in page.items]
    assert texts == [f"note {number:02d}" for number in range(25)]
    assert past_end.items == ()


@pytest.mark.asyncio
async def test_digest_never_mixes_spaces_bidirectionally(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_a = await add_capture(schema_engine, ACCESS_A)
    capture_b = await add_capture(schema_engine, ACCESS_B)
    note_a = await add_record(
        schema_engine,
        ACCESS_A,
        capture_a,
        SearchRecordType.NOTE,
        "приватно A",
        AS_OF - timedelta(hours=1),
    )
    note_b = await add_record(
        schema_engine,
        ACCESS_B,
        capture_b,
        SearchRecordType.NOTE,
        "приватно B",
        AS_OF - timedelta(hours=1),
    )

    page_a = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)
    page_b = await read_digest(engine, ACCESS_B, DigestPeriod.WEEK, 0, AS_OF)

    assert [item.id for item in page_a.items] == [note_a]
    assert [item.id for item in page_b.items] == [note_b]
    assert page_a.counters.notes == 1
    assert page_b.counters.notes == 1


@pytest.mark.asyncio
async def test_empty_period_returns_zero_counters_and_no_items(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture = await add_capture(schema_engine, ACCESS_A)
    # Запись есть, но ВНЕ выбранного периода.
    await add_record(
        schema_engine,
        ACCESS_A,
        capture,
        SearchRecordType.NOTE,
        "до начала недели",
        WEEK_START_UTC - timedelta(days=1),
    )

    page = await read_digest(engine, ACCESS_A, DigestPeriod.WEEK, 0, AS_OF)

    assert page.total == 0
    assert page.items == ()
    assert page.counters == DigestCounters(
        notes=0, tasks=0, tasks_completed=0, ideas=0, decisions=0, questions=0
    )

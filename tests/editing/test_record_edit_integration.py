"""Правка записи (S3) на живом PostgreSQL: полный конвейер одним коммитом.

Кнопка «✏️ Править» → pending-режим → следующее сообщение = новый текст:
UPDATE текста + INDEXING-only прогон (без пере-классификации) + пересбор
sidecar-ссылок. Журнал CaptureEvent неизменяем; напоминания правкой не
трогаются; время из нового текста НЕ извлекается.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.indexing_completion import (
    CompleteIndexingCommand,
    IndexingCompletionInTransaction,
)
from second_brain.bootstrap.indexing_source import (
    PostgresIndexingSourceReader,
    ReadIndexingSourceCommand,
)
from second_brain.bootstrap.indexing_worker import IndexingWorker
from second_brain.bootstrap.record_edit_in_transaction import RecordEditInTransaction
from second_brain.bootstrap.record_view_in_transaction import RecordViewInTransaction
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.application.contracts import TelegramLink
from second_brain.slices.editing.adapters.persistence.models import (
    PendingEditModeModel,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingRunModel,
    ProcessingStepModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepType,
    TranscriptionOutputType,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.reminders.domain.entities import ReminderStatus
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
    SemanticDocumentModel,
)
from second_brain.slices.retrieval.application.indexing import IndexSource
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.weblinks.adapters.persistence.models import RecordUrlModel
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.embedding_fakes import FakeEmbeddingModel

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
SPACE_ID = UUID("00000000-0000-0000-0000-000000000011")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
OTHER_SPACE_ID = UUID("00000000-0000-0000-0000-000000000012")
ORIGINAL_TEXT = "первая версия, смотри тут"
EDITED_TEXT = "правленая версия, теперь здесь"
ORIGINAL_LINKS = (TelegramLink(label="тут", url="https://old.example/a"),)
EDITED_LINKS = (TelegramLink(label="здесь", url="https://new.example/b"),)
ACCESS = AccessContext(user_id=USER_ID, user_space_id=SPACE_ID)
LEASE = timedelta(minutes=15)


class SteppingClock:
    """Каждый вызов now() — новый момент: created_at и updated_at различимы
    (равные метки превращали бы «(изменено)» и порядок в лотерею)."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def now(self) -> datetime:
        current = self._now
        self._now = current + timedelta(minutes=1)
        return current


@pytest_asyncio.fixture(autouse=True)
async def reset_editing_schema(
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
                    "id": user_id,
                    "role": "admin" if user_id == USER_ID else "member",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for user_id in (USER_ID, OTHER_USER_ID)
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                {
                    "id": space_id,
                    "owner_user_id": user_id,
                    "timezone": "Asia/Jerusalem",
                    "language": "ru",
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for space_id, user_id in (
                    (SPACE_ID, USER_ID),
                    (OTHER_SPACE_ID, OTHER_USER_ID),
                )
            ],
        )
        await connection.execute(
            insert(TelegramIdentity),
            [
                {
                    "id": UUID(int=identity_seed),
                    "telegram_user_id": telegram_user_id,
                    "user_id": user_id,
                    "is_active": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for identity_seed, telegram_user_id, user_id in (
                    (0x21, 42, USER_ID),
                    (0x22, 43, OTHER_USER_ID),
                )
            ],
        )


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    transaction_port = TaskCaptureInTransaction()
    record_view = RecordViewInTransaction()
    return LocalUpdateProcessor(
        PostgresUpdateRepository(create_session_factory(engine)),
        SteppingClock(),
        b"test-pepper",
        "test-key",
        capture_text_port=transaction_port,
        task_mode_port=transaction_port,
        task_panel_port=transaction_port,
        reminder_ack_port=transaction_port,
        record_view_port=record_view,
        record_links_port=record_view,
        record_edit_port=RecordEditInTransaction(),
    )


def text_update(
    update_id: int,
    value: str,
    links: tuple[TelegramLink, ...] = (),
    telegram_user_id: int = 42,
) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=telegram_user_id,
        text=value,
        telegram_message_id=update_id + 1_000,
        links=links,
    )


def callback(update_id: int, data: str, telegram_user_id: int = 42) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=telegram_user_id,
        text=None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


async def _single_note(schema_engine: AsyncEngine) -> NoteModel:
    async with create_session_factory(schema_engine)() as session:
        return (await session.execute(select(NoteModel))).scalars().one()


async def _single_task(schema_engine: AsyncEngine) -> TaskModel:
    async with create_session_factory(schema_engine)() as session:
        return (await session.execute(select(TaskModel))).scalars().one()


async def _capture_note(
    app: LocalUpdateProcessor, schema_engine: AsyncEngine, update_id: int
) -> NoteModel:
    result = await app.process(
        text_update(update_id, ORIGINAL_TEXT, links=ORIGINAL_LINKS)
    )
    assert result.kind is AcknowledgementKind.CAPTURED
    return await _single_note(schema_engine)


async def _edit_record(
    app: LocalUpdateProcessor,
    *,
    kind: str,
    record_id: UUID,
    first_update_id: int,
    new_text: str,
    links: tuple[TelegramLink, ...] = (),
) -> object:
    begin = await app.process(callback(first_update_id, f"edit:{kind}:{record_id}"))
    assert begin.kind is AcknowledgementKind.EDIT_MODE_SET
    return await app.process(text_update(first_update_id + 1, new_text, links=links))


@pytest.mark.asyncio
async def test_edit_replaces_text_reindexes_and_rebuilds_links(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 100)

    result = await _edit_record(
        app,
        kind="note",
        record_id=note.id,
        first_update_id=101,
        new_text=EDITED_TEXT,
        links=EDITED_LINKS,
    )

    assert result.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]
    async with create_session_factory(schema_engine)() as session:
        edited = (await session.execute(select(NoteModel))).scalars().one()
        source = (await session.execute(select(CaptureEventModel))).scalars().one()
        runs = (
            (
                await session.execute(
                    select(ProcessingRunModel).order_by(ProcessingRunModel.version)
                )
            )
            .scalars()
            .all()
        )
        steps_of_reindex = (
            (
                await session.execute(
                    select(ProcessingStepModel).where(
                        ProcessingStepModel.processing_run_id == runs[-1].id
                    )
                )
            )
            .scalars()
            .all()
        )
        targets = (await session.execute(select(IndexingTargetModel))).scalars().all()
        urls = (
            (
                await session.execute(
                    select(RecordUrlModel).order_by(RecordUrlModel.position)
                )
            )
            .scalars()
            .all()
        )
        pending_count = await session.scalar(
            select(func.count()).select_from(PendingEditModeModel)
        )
    # Текст записи заменён, правка видима по updated_at; журнал — неизменяем.
    assert edited.text == EDITED_TEXT
    assert edited.updated_at > edited.created_at
    assert source.raw_text == ORIGINAL_TEXT
    # Пере-индексация БЕЗ пере-классификации: новый прогон version=2 с
    # ЕДИНСТВЕННЫМ шагом INDEXING, цель — правленая запись.
    assert [run.version for run in runs] == [1, 2]
    assert runs[-1].output_type is TranscriptionOutputType.NOTE
    assert [step.step_type for step in steps_of_reindex] == [
        ProcessingStepType.INDEXING
    ]
    reindex_targets = [
        target for target in targets if target.processing_run_id == runs[-1].id
    ]
    assert [(t.record_kind, t.record_id) for t in reindex_targets] == [
        (SearchRecordType.NOTE, note.id)
    ]
    # Sidecar-ссылки пересобраны под НОВЫЙ текст (замена набора, позиции с 0).
    assert [(row.position, row.label, row.url) for row in urls] == [
        (0, "здесь", "https://new.example/b")
    ]
    # Режим потреблён.
    assert pending_count == 0


@pytest.mark.asyncio
async def test_reindex_after_edit_replaces_chunks_without_duplicates(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Индексируем СТАРЫЙ текст воркером, правим запись, прогоняем воркер по
    # новому INDEXING-шагу: чанки отражают НОВЫЙ текст, старых нет, дублей нет.
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 200)
    session_factory = create_session_factory(engine)
    repository = PostgresProcessingRepository(session_factory)
    worker = IndexingWorker(
        queue=repository,
        source_reader=PostgresIndexingSourceReader(session_factory),
        indexer=IndexSource(FakeEmbeddingModel()),
        completion=IndexingCompletionInTransaction(session_factory),
    )
    assert await worker.process_once(ACCESS, NOW + timedelta(hours=1)) is True

    async with create_session_factory(schema_engine)() as session:
        old_chunks = (
            (await session.execute(select(SemanticDocumentModel))).scalars().all()
        )
    assert [row.chunk_text for row in old_chunks] == [ORIGINAL_TEXT]

    edited = await _edit_record(
        app,
        kind="note",
        record_id=note.id,
        first_update_id=201,
        new_text=EDITED_TEXT,
    )
    assert edited.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]
    assert await worker.process_once(ACCESS, NOW + timedelta(hours=2)) is True
    # Больше нечего индексировать: оба шага завершены.
    assert await worker.process_once(ACCESS, NOW + timedelta(hours=3)) is False

    async with create_session_factory(schema_engine)() as session:
        new_chunks = (
            (await session.execute(select(SemanticDocumentModel))).scalars().all()
        )
    assert [row.chunk_text for row in new_chunks] == [EDITED_TEXT]
    assert [row.source_record_id for row in new_chunks] == [note.id]


@pytest.mark.asyncio
async def test_edit_to_text_without_links_clears_the_sidecar(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 110)

    result = await _edit_record(
        app,
        kind="note",
        record_id=note.id,
        first_update_id=111,
        new_text="без ссылок",
        links=(),
    )

    assert result.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]
    async with create_session_factory(schema_engine)() as session:
        url_count = await session.scalar(
            select(func.count()).select_from(RecordUrlModel)
        )
    assert url_count == 0


@pytest.mark.asyncio
async def test_edit_of_task_with_pending_reminder_keeps_the_alarm(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    captured = await app.process(text_update(120, "позвонить Ави завтра в 10:00"))
    assert captured.kind is AcknowledgementKind.CAPTURED
    assert captured.reminder_when is not None
    task = await _single_task(schema_engine)
    async with create_session_factory(schema_engine)() as session:
        reminder_before = (await session.execute(select(ReminderModel))).scalars().one()

    result = await _edit_record(
        app,
        kind="task",
        record_id=task.id,
        first_update_id=121,
        new_text="позвонить Авиву завтра в 10:00",
    )

    assert result.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]
    async with create_session_factory(schema_engine)() as session:
        edited_task = (await session.execute(select(TaskModel))).scalars().one()
        reminder_after = (await session.execute(select(ReminderModel))).scalars().one()
    # Будильник действительно не сдвинулся и не задвоился (решение §6.2).
    assert edited_task.title == "позвонить Авиву завтра в 10:00"
    assert reminder_after.id == reminder_before.id
    assert reminder_after.remind_at == reminder_before.remind_at
    assert reminder_after.status is ReminderStatus.PENDING
    # Ack несёт «на когда» ЖИВОЕ напоминание — момент в tz пространства.
    reminder_when = result.reminder_when  # type: ignore[attr-defined]
    assert reminder_when == reminder_before.remind_at.astimezone(
        ZoneInfo("Asia/Jerusalem")
    )
    # Текст напоминания — снапшот на момент создания задачи.
    assert reminder_after.text == "позвонить Ави завтра в 10:00"


@pytest.mark.asyncio
async def test_edit_never_extracts_time_from_the_new_text(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Задача БЕЗ времени (через кнопку «Задача») → правка с явным временем:
    # напоминание НЕ создаётся — контекст произнесения потерян (решение §6.2).
    app = processor(engine)
    mode_set = await app.process(callback(130, "task:await_text"))
    assert mode_set.kind is AcknowledgementKind.TASK_MODE_SET
    captured = await app.process(text_update(131, "написать отчёт"))
    assert captured.kind is AcknowledgementKind.CAPTURED
    task = await _single_task(schema_engine)

    result = await _edit_record(
        app,
        kind="task",
        record_id=task.id,
        first_update_id=132,
        new_text="написать отчёт завтра в 09:00",
    )

    assert result.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]
    assert result.reminder_when is None  # type: ignore[attr-defined]
    async with create_session_factory(schema_engine)() as session:
        edited_task = (await session.execute(select(TaskModel))).scalars().one()
        reminder_count = await session.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert edited_task.title == "написать отчёт завтра в 09:00"
    assert reminder_count == 0


@pytest.mark.asyncio
async def test_edit_replay_of_the_same_update_applies_once(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 140)
    first = await _edit_record(
        app,
        kind="note",
        record_id=note.id,
        first_update_id=141,
        new_text=EDITED_TEXT,
    )
    assert first.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]

    # Повтор ТОГО ЖЕ апдейта (тот же update_id): receipt гасит второй проход.
    replay = await app.process(text_update(142, EDITED_TEXT))

    assert replay.kind is AcknowledgementKind.RECORD_EDITED
    assert replay.fresh is False
    async with create_session_factory(schema_engine)() as session:
        run_count = await session.scalar(
            select(func.count()).select_from(ProcessingRunModel)
        )
        edited = (await session.execute(select(NoteModel))).scalars().one()
    # Ровно один reindex-прогон (плюс исходный captured-прогон), текст один раз.
    assert run_count == 2
    assert edited.text == EDITED_TEXT


@pytest.mark.asyncio
async def test_editing_a_foreign_record_is_ignored_and_sets_no_mode(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 150)

    foreign = await app.process(
        callback(151, f"edit:note:{note.id}", telegram_user_id=43)
    )

    assert foreign.kind is AcknowledgementKind.IGNORED
    async with create_session_factory(schema_engine)() as session:
        pending_count = await session.scalar(
            select(func.count()).select_from(PendingEditModeModel)
        )
    assert pending_count == 0
    # Следующий текст чужака — обычная капча в ЕГО пространстве, не правка.
    captured = await app.process(text_update(152, "чужая заметка", telegram_user_id=43))
    assert captured.kind is AcknowledgementKind.CAPTURED
    async with create_session_factory(schema_engine)() as session:
        original = (
            (await session.execute(select(NoteModel).where(NoteModel.id == note.id)))
            .scalars()
            .one()
        )
    assert original.text == ORIGINAL_TEXT


@pytest.mark.asyncio
async def test_show_full_after_edit_shows_new_text_links_and_edited_mark(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 160)
    edited = await _edit_record(
        app,
        kind="note",
        record_id=note.id,
        first_update_id=161,
        new_text=EDITED_TEXT,
        links=EDITED_LINKS,
    )
    assert edited.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]

    shown = await app.process(callback(163, f"show:note:{note.id}"))

    assert shown.kind is AcknowledgementKind.RECORD_SHOWN
    record_view = shown.record_view
    assert record_view is not None
    assert record_view.record.text == EDITED_TEXT
    assert record_view.record.edited is True
    assert [(link.label, link.url) for link in record_view.links] == [
        ("здесь", "https://new.example/b")
    ]


@pytest.mark.asyncio
async def test_show_full_of_untouched_record_carries_no_edited_mark(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 170)

    shown = await app.process(callback(171, f"show:note:{note.id}"))

    assert shown.kind is AcknowledgementKind.RECORD_SHOWN
    assert shown.record_view is not None
    assert shown.record_view.record.edited is False


@pytest.mark.asyncio
async def test_late_primary_indexing_cannot_resurrect_the_old_text(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Гонка: первичный INDEXING прочитал СТАРЫЙ текст, но его completion
    # пришёл ПОСЛЕ правки и её пере-индексации. Устаревший результат не должен
    # затереть свежие чанки — шаг завершается БЕЗ записи.
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 300)
    session_factory = create_session_factory(engine)
    repository = PostgresProcessingRepository(session_factory)
    claim = await repository.claim_due_step(
        ACCESS, NOW + timedelta(hours=1), LEASE, (ProcessingStepType.INDEXING,)
    )
    assert claim is not None
    source = await PostgresIndexingSourceReader(session_factory).read(
        ReadIndexingSourceCommand(access_context=ACCESS, processing_run_id=claim.run_id)
    )
    stale_outcome = await IndexSource(FakeEmbeddingModel()).execute(source)

    # Правка и её пере-индексация успевают раньше позднего completion.
    edited = await _edit_record(
        app,
        kind="note",
        record_id=note.id,
        first_update_id=301,
        new_text=EDITED_TEXT,
    )
    assert edited.kind is AcknowledgementKind.RECORD_EDITED  # type: ignore[attr-defined]
    worker = IndexingWorker(
        queue=repository,
        source_reader=PostgresIndexingSourceReader(session_factory),
        indexer=IndexSource(FakeEmbeddingModel()),
        completion=IndexingCompletionInTransaction(session_factory),
    )
    # Лиза первичного шага ещё жива → воркер берёт шаг reindex-прогона.
    assert (
        await worker.process_once(ACCESS, NOW + timedelta(hours=1, minutes=1)) is True
    )

    await IndexingCompletionInTransaction(session_factory).complete(
        CompleteIndexingCommand(
            access_context=ACCESS,
            step_id=claim.step_id,
            outcome=stale_outcome,
            completed_at=NOW + timedelta(hours=1, minutes=2),
        )
    )

    async with create_session_factory(schema_engine)() as session:
        chunks = (await session.execute(select(SemanticDocumentModel))).scalars().all()
    # Поиск видит ТОЛЬКО текущий текст; старый не воскрес.
    assert [row.chunk_text for row in chunks] == [EDITED_TEXT]
    stale_run = await repository.get_run(ACCESS, claim.run_id)
    assert stale_run is not None
    stale_step = next(
        step
        for step in stale_run.steps
        if step.step_type is ProcessingStepType.INDEXING
    )
    # Устаревший шаг завершён успешно (актуальный run уже отразил текст) —
    # без ретраев и без записи чанков.
    assert stale_step.status.name == "SUCCEEDED"


@pytest.mark.asyncio
async def test_completed_task_without_edit_carries_no_edited_mark(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Завершение задачи двигает updated_at, но это НЕ правка текста:
    # пометки «(изменено)» быть не должно.
    app = processor(engine)
    mode_set = await app.process(callback(310, "task:await_text"))
    assert mode_set.kind is AcknowledgementKind.TASK_MODE_SET
    captured = await app.process(text_update(311, "сделать дело"))
    assert captured.kind is AcknowledgementKind.CAPTURED
    task = await _single_task(schema_engine)
    completed = await app.process(callback(312, f"tasks:complete:{task.id}"))
    assert completed.kind is AcknowledgementKind.TASK_COMPLETED

    shown = await app.process(callback(313, f"show:task:{task.id}"))

    assert shown.kind is AcknowledgementKind.RECORD_SHOWN
    assert shown.record_view is not None
    assert shown.record_view.record.edited is False


@pytest.mark.asyncio
async def test_slash_command_clears_the_edit_mode(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # «/start» (и любая /команда) — не новый текст записи: режим гасится,
    # следующее сообщение — обычная капча, запись не тронута.
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 320)
    begin = await app.process(callback(321, f"edit:note:{note.id}"))
    assert begin.kind is AcknowledgementKind.EDIT_MODE_SET

    await app.process(text_update(322, "/start"))

    async with create_session_factory(schema_engine)() as session:
        pending_count = await session.scalar(
            select(func.count()).select_from(PendingEditModeModel)
        )
    assert pending_count == 0
    captured = await app.process(text_update(323, "обычная заметка после команды"))
    assert captured.kind is AcknowledgementKind.CAPTURED
    async with create_session_factory(schema_engine)() as session:
        original = (
            (await session.execute(select(NoteModel).where(NoteModel.id == note.id)))
            .scalars()
            .one()
        )
        note_count = await session.scalar(select(func.count()).select_from(NoteModel))
    assert original.text == ORIGINAL_TEXT
    assert note_count == 2


@pytest.mark.asyncio
async def test_whitespace_only_text_does_not_apply_the_edit(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # «   » — не новый текст: запись и ссылки не тронуты, INDEXING-прогон не
    # рождается, режим остаётся ждать настоящий текст (тот же промпт).
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 330)
    begin = await app.process(callback(331, f"edit:note:{note.id}"))
    assert begin.kind is AcknowledgementKind.EDIT_MODE_SET

    blank = await app.process(text_update(332, "   "))

    assert blank.kind is AcknowledgementKind.EDIT_MODE_SET
    async with create_session_factory(schema_engine)() as session:
        untouched = (await session.execute(select(NoteModel))).scalars().one()
        run_count = await session.scalar(
            select(func.count()).select_from(ProcessingRunModel)
        )
        url_count = await session.scalar(
            select(func.count()).select_from(RecordUrlModel)
        )
        pending_count = await session.scalar(
            select(func.count()).select_from(PendingEditModeModel)
        )
    assert untouched.text == ORIGINAL_TEXT
    assert run_count == 1
    assert url_count == len(ORIGINAL_LINKS)
    assert pending_count == 1
    # Настоящий текст после пробельного — применяется ДОСЛОВНО.
    applied = await app.process(text_update(333, f"  {EDITED_TEXT}  "))
    assert applied.kind is AcknowledgementKind.RECORD_EDITED
    async with create_session_factory(schema_engine)() as session:
        edited = (await session.execute(select(NoteModel))).scalars().one()
    assert edited.text == f"  {EDITED_TEXT}  "


@pytest.mark.asyncio
async def test_edit_cancel_keeps_the_text_capturing_as_usual(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    note = await _capture_note(app, schema_engine, 180)
    begin = await app.process(callback(181, f"edit:note:{note.id}"))
    assert begin.kind is AcknowledgementKind.EDIT_MODE_SET

    cancelled = await app.process(callback(182, "edit:cancel"))

    assert cancelled.kind is AcknowledgementKind.EDIT_MODE_CANCELLED
    captured = await app.process(text_update(183, "новая отдельная заметка"))
    assert captured.kind is AcknowledgementKind.CAPTURED
    async with create_session_factory(schema_engine)() as session:
        texts = (await session.scalars(select(NoteModel.text))).all()
    assert sorted(texts) == sorted([ORIGINAL_TEXT, "новая отдельная заметка"])

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.memory_delivery import (
    DELIVERY_FAILURE_CODE,
    AiogramAnswerDelivery,
    CompleteMemoryDeliveryCommand,
    MemoryDeliveryCompletionInTransaction,
)
from second_brain.bootstrap.memory_reasoning_completion import (
    CompleteMemoryReasoningCommand,
    MemoryReasoningCompletionInTransaction,
)
from second_brain.bootstrap.memory_retrieval_completion import (
    CompleteMemoryRetrievalCommand,
    MemoryRetrievalCompletionInTransaction,
)
from second_brain.bootstrap.memory_worker import MemoryWorker
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.identity.application.local_updates import (
    _SHOW_CALLBACK_PATTERN,
)
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.memory.adapters.persistence.models import (
    MemoryAnswerModel,
    MemoryAnswerRunModel,
    MemoryAnswerStepModel,
    MemoryRunEvidenceModel,
)
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryQueue,
)
from second_brain.slices.memory.application.answer_question import AnswerMemoryQuestion
from second_brain.slices.memory.application.contracts import (
    AnswerSourceRef,
    DeliveryPayload,
    ReasoningDraft,
    ReasoningRequest,
)
from second_brain.slices.memory.application.render import (
    render_answer,
    render_safe_failure,
    render_source_label,
)
from second_brain.slices.memory.application.structured_output import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
)
from second_brain.slices.memory.domain.entities import (
    EvidenceLevel,
    MemoryAnswer,
    MemoryRecordKind,
    MemoryRunStatus,
    MemoryStepType,
)
from second_brain.slices.memory.ports.repositories import CreateMemoryQuestionCommand
from second_brain.slices.retrieval.adapters.embedding.e5 import EmbeddingFailure
from tests.identity.conftest import IsolatedDatabase
from tests.retrieval.embedding_fakes import FakeEmbeddingModel

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
QUESTION = "постгрес"
CLEAN_TEXT = "постгрес чистая заметка про индексы"
SECRET_TEXT = "постгрес доступ token=SUPERSECRETVALUE1234"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}")
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_memory_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [_user(ACCESS_A), _user(ACCESS_B)])
        await connection.execute(
            insert(UserSpace), [_space(ACCESS_A), _space(ACCESS_B)]
        )


def _user(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_id,
        "role": "member",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _space(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_space_id,
        "owner_user_id": access.user_id,
        "timezone": "Asia/Jerusalem",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _trace(update_id: int) -> str:
    return f"{update_id:x}".rjust(32, "1")[-32:]


async def _add_note(
    schema_engine: AsyncEngine, access: AccessContext, content: str
) -> tuple[UUID, UUID]:
    source_id = uuid4()
    record_id = uuid4()
    update_id = int(source_id.int % 9_000_000_000) + 1
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=source_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 10_000_000_000,
                raw_text=content,
                received_at=NOW,
                created_at=NOW,
                trace_id=_trace(update_id),
            )
        )
        await connection.execute(
            insert(NoteModel).values(
                id=record_id,
                user_space_id=access.user_space_id,
                source_capture_event_id=source_id,
                text=content,
                created_at=NOW,
                updated_at=NOW,
                trace_id=_trace(update_id),
            )
        )
    return record_id, source_id


def _question_command(
    access: AccessContext,
    *,
    update_id: int,
    question_text: str = QUESTION,
) -> CreateMemoryQuestionCommand:
    return CreateMemoryQuestionCommand(
        access_context=access,
        bot_id=100,
        telegram_update_id=update_id,
        question_text=question_text,
        current_project_id=None,
        created_at=NOW,
        trace_id=_trace(update_id),
    )


async def _create_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
    question_text: str = QUESTION,
) -> UUID:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(
        _question_command(access, update_id=update_id, question_text=question_text)
    )
    async with schema_engine.connect() as connection:
        run_id = await connection.scalar(
            select(MemoryAnswerRunModel.id).where(
                MemoryAnswerRunModel.question_id == question.id
            )
        )
    assert run_id is not None
    return run_id


async def _step_statuses(
    schema_engine: AsyncEngine, run_id: UUID
) -> dict[MemoryStepType, MemoryRunStatus]:
    async with schema_engine.connect() as connection:
        rows = (
            await connection.execute(
                select(
                    MemoryAnswerStepModel.step_type,
                    MemoryAnswerStepModel.status,
                ).where(MemoryAnswerStepModel.run_id == run_id)
            )
        ).all()
    return {row[0]: MemoryRunStatus(row[1]) for row in rows}


async def _reasoning_attempts(schema_engine: AsyncEngine, run_id: UUID) -> int:
    async with schema_engine.connect() as connection:
        attempts = await connection.scalar(
            select(MemoryAnswerStepModel.attempt_count).where(
                MemoryAnswerStepModel.run_id == run_id,
                MemoryAnswerStepModel.step_type == MemoryStepType.REASONING,
            )
        )
    assert attempts is not None
    return attempts


async def _reasoning_next_attempt(
    schema_engine: AsyncEngine, run_id: UUID
) -> datetime | None:
    async with schema_engine.connect() as connection:
        return await connection.scalar(
            select(MemoryAnswerStepModel.next_attempt_at).where(
                MemoryAnswerStepModel.run_id == run_id,
                MemoryAnswerStepModel.step_type == MemoryStepType.REASONING,
            )
        )


async def _evidence_rows(
    schema_engine: AsyncEngine, run_id: UUID
) -> list[MemoryRunEvidenceModel]:
    async with create_session_factory(schema_engine)() as session:
        rows = (
            await session.execute(
                select(MemoryRunEvidenceModel)
                .where(MemoryRunEvidenceModel.run_id == run_id)
                .order_by(MemoryRunEvidenceModel.label)
            )
        ).scalars()
        return list(rows)


async def _answer_rows(schema_engine: AsyncEngine, run_id: UUID) -> int:
    async with schema_engine.connect() as connection:
        rows = await connection.execute(
            select(MemoryAnswerModel).where(MemoryAnswerModel.run_id == run_id)
        )
        return len(rows.all())


class FakeReasoningModel:
    """Records every request and answers by echoing the request's own labels so
    the contract always validates. Configurable to raise instead."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        evidence_level: EvidenceLevel = EvidenceLevel.DIRECT,
        answer: str = "Собранный ответ из памяти.",
    ) -> None:
        self.requests: list[ReasoningRequest] = []
        self._error = error
        self._level = evidence_level
        self._answer = answer

    async def reason(self, request: ReasoningRequest) -> ReasoningDraft:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        labels = tuple(snippet.label for snippet in request.snippets)
        return ReasoningDraft(
            model_name="fake-reasoner",
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            evidence_level=self._level,
            answer=self._answer,
            source_labels=labels[:1],
        )


class FakeAnswerDeliveryPort:
    def __init__(self) -> None:
        self.deliveries: list[tuple[DeliveryPayload, TelegramRecipient]] = []

    async def deliver(
        self, payload: DeliveryPayload, recipient_context: TelegramRecipient
    ) -> None:
        self.deliveries.append((payload, recipient_context))


class FakeWorkerIdentity:
    def __init__(
        self, telegram_user_id: int = 777_001, locale: Locale = Locale.RU
    ) -> None:
        self._telegram_user_id = telegram_user_id
        self._locale = locale
        self.calls: list[AccessContext] = []
        self.locale_calls: list[AccessContext] = []

    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        raise AssertionError("worker identity listing is not used in these tests")

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        self.calls.append(access_context)
        return TelegramRecipient(telegram_user_id=self._telegram_user_id)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        self.locale_calls.append(access_context)
        return self._locale


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append((args, kwargs))


def _build_worker(
    engine: AsyncEngine,
    *,
    reasoner: FakeReasoningModel,
    delivery_port: FakeAnswerDeliveryPort,
    identity: FakeWorkerIdentity,
    embedding_model: FakeEmbeddingModel | None = None,
) -> tuple[PostgresMemoryQueue, MemoryWorker]:
    session_factory = create_session_factory(engine)
    queue = PostgresMemoryQueue(session_factory)
    worker = MemoryWorker(
        queue=queue,
        retrieval=MemoryRetrievalCompletionInTransaction(
            session_factory, embedding_model or FakeEmbeddingModel()
        ),
        reasoning=MemoryReasoningCompletionInTransaction(
            session_factory, AnswerMemoryQuestion(reasoner)
        ),
        delivery=MemoryDeliveryCompletionInTransaction(
            session_factory, delivery_port, identity
        ),
        step_lease=LEASE,
    )
    return queue, worker


@pytest.mark.asyncio
async def test_retrieval_snapshots_evidence_and_excludes_secret(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    clean_id, _ = await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    await _add_note(schema_engine, ACCESS_A, SECRET_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=101)
    _, worker = _build_worker(
        engine,
        reasoner=FakeReasoningModel(),
        delivery_port=FakeAnswerDeliveryPort(),
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True

    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.RETRIEVAL] is MemoryRunStatus.SUCCEEDED
    assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.PENDING

    rows = await _evidence_rows(schema_engine, run_id)
    assert rows != []
    assert all("SUPERSECRETVALUE1234" not in row.snippet_text for row in rows)
    assert all("token=" not in row.snippet_text for row in rows)
    assert {row.record_id for row in rows} == {clean_id}
    assert [row.label for row in rows] == [
        f"S{index + 1}" for index in range(len(rows))
    ]


@pytest.mark.asyncio
async def test_reasoning_reads_snapshot_not_live_index(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    snapshot_id, _ = await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=102)
    reasoner = FakeReasoningModel()
    queue, worker = _build_worker(
        engine,
        reasoner=reasoner,
        delivery_port=FakeAnswerDeliveryPort(),
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval

    # The index grows AFTER the snapshot with an even more relevant record.
    late_id, _ = await _add_note(
        schema_engine, ACCESS_A, "постгрес постгрес постгрес свежая запись"
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # reasoning

    assert len(reasoner.requests) == 1
    fed_texts = [snippet.text for snippet in reasoner.requests[0].snippets]
    assert fed_texts == [CLEAN_TEXT]
    stored = await queue.read_answer(ACCESS_A, run_id)
    assert stored is not None
    source_ids = {source.record_id for source in stored.sources}
    assert source_ids == {snapshot_id}
    assert late_id not in source_ids


@pytest.mark.asyncio
async def test_insufficient_snapshot_skips_provider(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # No seeded evidence: retrieval snapshots nothing.
    run_id = await _create_run(
        engine, schema_engine, ACCESS_A, update_id=103, question_text="ничего похожего"
    )
    reasoner = FakeReasoningModel()
    delivery_port = FakeAnswerDeliveryPort()
    _, worker = _build_worker(
        engine,
        reasoner=reasoner,
        delivery_port=delivery_port,
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval
    assert await _evidence_rows(schema_engine, run_id) == []

    assert await worker.process_once(ACCESS_A, NOW) is True  # reasoning
    assert reasoner.requests == []
    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.SUCCEEDED

    assert await worker.process_once(ACCESS_A, NOW) is True  # delivery
    payload, _ = delivery_port.deliveries[-1]
    assert payload.text == render_answer(
        MemoryAnswer(
            evidence_level=EvidenceLevel.INSUFFICIENT,
            answer_text="",
            sources=(),
            model_name=None,
            prompt_version=None,
            schema_version=None,
        ),
        Locale.RU,
    )
    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.DELIVERY] is MemoryRunStatus.SUCCEEDED
    # ∅-answer carries no source refs, so the sent message gets no buttons.
    assert payload.sources == ()


@pytest.mark.asyncio
async def test_reasoner_payload_is_only_question_and_scoped_snippets(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    await _add_note(
        schema_engine, ACCESS_B, "постгрес чужой секрет другого пользователя"
    )
    await _create_run(engine, schema_engine, ACCESS_A, update_id=104)
    reasoner = FakeReasoningModel()
    _, worker = _build_worker(
        engine,
        reasoner=reasoner,
        delivery_port=FakeAnswerDeliveryPort(),
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval
    assert await worker.process_once(ACCESS_A, NOW) is True  # reasoning

    assert len(reasoner.requests) == 1
    request = reasoner.requests[0]
    assert request.question == QUESTION
    assert all(re.fullmatch(r"S\d+", snippet.label) for snippet in request.snippets)
    joined = " ".join(snippet.text for snippet in request.snippets)
    assert "чужой" not in joined
    assert CLEAN_TEXT in joined
    rendered = repr(request)
    for fragment in (QUESTION, CLEAN_TEXT, str(ACCESS_A.user_space_id)):
        assert fragment not in rendered


@pytest.mark.asyncio
async def test_provider_failure_is_bounded_retry_on_reasoning_step(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=105)
    reasoner = FakeReasoningModel(error=RuntimeError("provider down"))
    _, worker = _build_worker(
        engine,
        reasoner=reasoner,
        delivery_port=FakeAnswerDeliveryPort(),
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval succeeds

    at = NOW
    for attempt in range(1, 4):
        assert await worker.process_once(ACCESS_A, at) is True
        assert await _reasoning_attempts(schema_engine, run_id) == attempt
        statuses = await _step_statuses(schema_engine, run_id)
        assert statuses[MemoryStepType.RETRIEVAL] is MemoryRunStatus.SUCCEEDED
        assert statuses[MemoryStepType.DELIVERY] is MemoryRunStatus.PENDING
        if attempt < 3:
            assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.PENDING
            next_at = await _reasoning_next_attempt(schema_engine, run_id)
            assert next_at is not None
            at = next_at

    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.FAILED
    assert await _answer_rows(schema_engine, run_id) == 0
    # captured content is untouched
    async with schema_engine.connect() as connection:
        note_count = await connection.scalar(
            select(NoteModel.id).where(NoteModel.text == CLEAN_TEXT)
        )
    assert note_count is not None


@pytest.mark.asyncio
async def test_delivery_delivers_safe_failure_when_reasoning_failed(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=106)
    delivery_port = FakeAnswerDeliveryPort()
    _, worker = _build_worker(
        engine,
        reasoner=FakeReasoningModel(error=RuntimeError("provider down")),
        delivery_port=delivery_port,
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval
    at = NOW
    for _ in range(3):  # exhaust reasoning attempts
        assert await worker.process_once(ACCESS_A, at) is True
        at = (await _reasoning_next_attempt(schema_engine, run_id) or at) + timedelta(
            seconds=1
        )

    assert await worker.process_once(ACCESS_A, at) is True  # delivery becomes due
    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.DELIVERY] is MemoryRunStatus.SUCCEEDED

    assert len(delivery_port.deliveries) == 1
    payload, _ = delivery_port.deliveries[0]
    assert payload.text == render_safe_failure(_trace(106), Locale.RU)
    assert payload.safe_error_code == DELIVERY_FAILURE_CODE
    assert payload.trace_id == _trace(106)
    assert await _answer_rows(schema_engine, run_id) == 0


@pytest.mark.asyncio
async def test_delivery_becomes_due_and_safe_when_retrieval_failed(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Pravka #1: retrieval exhausts its attempts -> FAILED. Reasoning can never
    # become due (it needs retrieval SUCCEEDED), so without the fix delivery
    # would never become due and the run would hang forever with the user
    # hearing nothing. Delivery MUST become due on a terminal upstream failure.
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=107)
    delivery_port = FakeAnswerDeliveryPort()
    _, worker = _build_worker(
        engine,
        reasoner=FakeReasoningModel(),
        delivery_port=delivery_port,
        identity=FakeWorkerIdentity(),
        embedding_model=FakeEmbeddingModel(error=EmbeddingFailure("embedding_failed")),
    )

    at = NOW
    for _ in range(3):  # exhaust retrieval attempts
        assert await worker.process_once(ACCESS_A, at) is True
        async with schema_engine.connect() as connection:
            next_at = await connection.scalar(
                select(MemoryAnswerStepModel.next_attempt_at).where(
                    MemoryAnswerStepModel.run_id == run_id,
                    MemoryAnswerStepModel.step_type == MemoryStepType.RETRIEVAL,
                )
            )
        at = (next_at or at) + timedelta(seconds=1)

    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.RETRIEVAL] is MemoryRunStatus.FAILED
    assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.PENDING

    assert await worker.process_once(ACCESS_A, at) is True  # delivery becomes due
    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.DELIVERY] is MemoryRunStatus.SUCCEEDED
    # reasoning was never claimed: retrieval never SUCCEEDED
    assert await _reasoning_attempts(schema_engine, run_id) == 0

    assert len(delivery_port.deliveries) == 1
    payload, _ = delivery_port.deliveries[0]
    assert payload.text == render_safe_failure(_trace(107), Locale.RU)
    assert payload.safe_error_code == DELIVERY_FAILURE_CODE
    assert payload.trace_id == _trace(107)


@pytest.mark.asyncio
async def test_success_delivers_render_answer_and_reasoner_called_once(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=108)
    reasoner = FakeReasoningModel()
    delivery_port = FakeAnswerDeliveryPort()
    identity = FakeWorkerIdentity(telegram_user_id=555_222)
    queue, worker = _build_worker(
        engine,
        reasoner=reasoner,
        delivery_port=delivery_port,
        identity=identity,
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval
    assert await worker.process_once(ACCESS_A, NOW) is True  # reasoning
    assert await worker.process_once(ACCESS_A, NOW) is True  # delivery

    assert len(reasoner.requests) == 1  # delivery never re-invokes the provider
    statuses = await _step_statuses(schema_engine, run_id)
    assert all(status is MemoryRunStatus.SUCCEEDED for status in statuses.values())

    stored = await queue.read_answer(ACCESS_A, run_id)
    assert stored is not None
    assert len(delivery_port.deliveries) == 1
    payload, recipient = delivery_port.deliveries[0]
    assert payload.text == render_answer(stored, Locale.RU)
    assert payload.safe_error_code is None
    assert recipient.telegram_user_id == 555_222
    assert identity.calls == [ACCESS_A]
    assert identity.locale_calls == [ACCESS_A]

    # No further step is due once the whole run has SUCCEEDED.
    assert await worker.process_once(ACCESS_A, NOW + timedelta(minutes=30)) is False


@pytest.mark.asyncio
async def test_delivery_renders_in_user_locale(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=110)
    delivery_port = FakeAnswerDeliveryPort()
    identity = FakeWorkerIdentity(locale=Locale.EN)
    queue, worker = _build_worker(
        engine,
        reasoner=FakeReasoningModel(),
        delivery_port=delivery_port,
        identity=identity,
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval
    assert await worker.process_once(ACCESS_A, NOW) is True  # reasoning
    assert await worker.process_once(ACCESS_A, NOW) is True  # delivery

    stored = await queue.read_answer(ACCESS_A, run_id)
    assert stored is not None
    payload, _ = delivery_port.deliveries[0]
    assert payload.text == render_answer(stored, Locale.EN)
    assert "Sources:" in (payload.text or "")
    assert identity.locale_calls == [ACCESS_A]


@pytest.mark.asyncio
async def test_process_once_advances_one_step_per_cycle(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=109)
    _, worker = _build_worker(
        engine,
        reasoner=FakeReasoningModel(),
        delivery_port=FakeAnswerDeliveryPort(),
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True
    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.RETRIEVAL] is MemoryRunStatus.SUCCEEDED
    assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.PENDING
    assert statuses[MemoryStepType.DELIVERY] is MemoryRunStatus.PENDING


@pytest.mark.asyncio
async def test_success_payload_carries_source_refs_for_show_buttons(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # The delivery payload carries (kind, record_id, label) per answer source so
    # the adapter can attach show:<type>:<uuid> buttons matching the sources.
    note_id, _ = await _add_note(schema_engine, ACCESS_A, CLEAN_TEXT)
    run_id = await _create_run(engine, schema_engine, ACCESS_A, update_id=111)
    delivery_port = FakeAnswerDeliveryPort()
    queue, worker = _build_worker(
        engine,
        reasoner=FakeReasoningModel(),
        delivery_port=delivery_port,
        identity=FakeWorkerIdentity(),
    )

    assert await worker.process_once(ACCESS_A, NOW) is True  # retrieval
    assert await worker.process_once(ACCESS_A, NOW) is True  # reasoning
    assert await worker.process_once(ACCESS_A, NOW) is True  # delivery

    stored = await queue.read_answer(ACCESS_A, run_id)
    assert stored is not None
    assert stored.sources != ()
    payload, _ = delivery_port.deliveries[0]
    assert [(ref.record_kind, ref.record_id) for ref in payload.sources] == [
        (source.record_kind, source.record_id) for source in stored.sources
    ]
    assert {ref.record_id for ref in payload.sources} == {note_id}
    for ref, source in zip(payload.sources, stored.sources, strict=True):
        assert ref.label == render_source_label(source, Locale.RU)
        assert ref.label in payload.text  # same line the message already shows
    # record ids stay out of reprs, like everywhere else in the slice.
    assert UUID_PATTERN.search(repr(payload)) is None


@pytest.mark.asyncio
async def test_aiogram_delivery_attaches_numbered_source_buttons() -> None:
    bot = RecordingBot()
    delivery = AiogramAnswerDelivery(bot)  # type: ignore[arg-type]
    record_ids = [uuid4() for _ in range(6)]
    kinds = [
        MemoryRecordKind.NOTE,
        MemoryRecordKind.TASK,
        MemoryRecordKind.IDEA,
        MemoryRecordKind.DECISION,
        MemoryRecordKind.QUESTION,
        MemoryRecordKind.NOTE,
    ]
    payload = DeliveryPayload.success(
        "готовый ответ",
        sources=tuple(
            AnswerSourceRef(record_kind=kind, record_id=record_id, label="Заметка")
            for kind, record_id in zip(kinds, record_ids, strict=True)
        ),
    )

    await delivery.deliver(payload, TelegramRecipient(telegram_user_id=42))

    assert len(bot.messages) == 1
    args, kwargs = bot.messages[0]
    assert args == (42, "готовый ответ")
    assert "parse_mode" not in kwargs
    markup = kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    rows = markup.inline_keyboard
    assert [len(row) for row in rows] == [5, 1]  # rows of 5, like search
    buttons = [button for row in rows for button in row]
    assert [button.text for button in buttons] == ["1", "2", "3", "4", "5", "6"]
    assert [button.callback_data for button in buttons] == [
        f"show:{kind.value}:{record_id}"
        for kind, record_id in zip(kinds, record_ids, strict=True)
    ]
    for button in buttons:
        assert button.callback_data is not None
        assert len(button.callback_data.encode()) <= 64  # Telegram limit
        # Every callback matches the strict, already-shipped show handler.
        assert _SHOW_CALLBACK_PATTERN.fullmatch(button.callback_data) is not None


@pytest.mark.asyncio
async def test_aiogram_delivery_without_sources_sends_no_reply_markup() -> None:
    bot = RecordingBot()
    delivery = AiogramAnswerDelivery(bot)  # type: ignore[arg-type]

    await delivery.deliver(
        DeliveryPayload.success("ответ без источников"),
        TelegramRecipient(telegram_user_id=42),
    )

    assert len(bot.messages) == 1
    args, kwargs = bot.messages[0]
    assert args == (42, "ответ без источников")
    assert "reply_markup" not in kwargs


@pytest.mark.asyncio
async def test_aiogram_delivery_sends_ready_text_without_rendering() -> None:
    # The completion renders (in the user's locale) before the adapter runs, so
    # the adapter never renders and simply forwards payload.text as plain text.
    bot = RecordingBot()
    delivery = AiogramAnswerDelivery(bot)  # type: ignore[arg-type]
    recipient = TelegramRecipient(telegram_user_id=42)

    await delivery.deliver(DeliveryPayload.success("готовый ответ"), recipient)
    failure = DeliveryPayload(
        text=render_safe_failure("abc123", Locale.EN),
        safe_error_code="code_x",
        trace_id="abc123",
    )
    await delivery.deliver(failure, recipient)

    assert len(bot.messages) == 2
    first_args, first_kwargs = bot.messages[0]
    assert first_args == (42, "готовый ответ")
    assert "parse_mode" not in first_kwargs
    second_args, second_kwargs = bot.messages[1]
    assert second_args == (42, render_safe_failure("abc123", Locale.EN))
    assert "parse_mode" not in second_kwargs


def test_memory_completion_commands_leak_no_content() -> None:
    retrieval = CompleteMemoryRetrievalCommand(
        access_context=ACCESS_A, step_id=uuid4(), run_id=uuid4(), completed_at=NOW
    )
    reasoning = CompleteMemoryReasoningCommand(
        access_context=ACCESS_A, step_id=uuid4(), run_id=uuid4(), completed_at=NOW
    )
    delivery = CompleteMemoryDeliveryCommand(
        access_context=ACCESS_A,
        step_id=uuid4(),
        run_id=uuid4(),
        trace_id=_trace(999),
        completed_at=NOW,
    )
    for value in (repr(retrieval), repr(reasoning), repr(delivery)):
        assert UUID_PATTERN.search(value) is None
        assert _trace(999) not in value

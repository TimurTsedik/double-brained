from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.adapters.persistence.models import (
    MemoryAnswerModel,
    MemoryAnswerRunModel,
    MemoryAnswerSourceModel,
    MemoryAnswerStepModel,
    MemoryQuestionModel,
)
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryQueue,
)
from second_brain.slices.memory.application.answer_question import AnswerMemoryQuestion
from second_brain.slices.memory.application.contracts import (
    ReasoningDraft,
    ReasoningRequest,
)
from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    EvidenceSnippet,
    MemoryAnswer,
    MemoryRecordKind,
    MemoryRunStatus,
    MemoryStepType,
    overall_status,
)
from second_brain.slices.memory.ports.repositories import (
    CreateMemoryQuestionCommand,
    FailMemoryStepCommand,
    SaveMemoryAnswerCommand,
    SnapshotEvidenceCommand,
    SucceedMemoryStepCommand,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
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


def _create_command(
    access: AccessContext, *, update_id: int, trace_seed: str = "1"
) -> CreateMemoryQuestionCommand:
    return CreateMemoryQuestionCommand(
        access_context=access,
        bot_id=100,
        telegram_update_id=update_id,
        question_text="что я решил про проект?",
        current_project_id=None,
        created_at=NOW,
        trace_id=f"{update_id:x}".rjust(32, trace_seed)[-32:],
    )


async def _run_id_for(schema_engine: AsyncEngine, question_id: UUID) -> UUID:
    async with schema_engine.connect() as connection:
        run_id = await connection.scalar(
            select(MemoryAnswerRunModel.id).where(
                MemoryAnswerRunModel.question_id == question_id
            )
        )
    assert run_id is not None
    return run_id


async def _step_id(
    schema_engine: AsyncEngine, run_id: UUID, step_type: MemoryStepType
) -> UUID:
    async with schema_engine.connect() as connection:
        step_id = await connection.scalar(
            select(MemoryAnswerStepModel.id).where(
                MemoryAnswerStepModel.run_id == run_id,
                MemoryAnswerStepModel.step_type == step_type,
            )
        )
    assert step_id is not None
    return step_id


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


@pytest.mark.asyncio
async def test_record_kind_pins_retrieval_search_record_type() -> None:
    # Evidence carries a retrieval SearchRecordType record_kind that gets copied
    # into the memory MemoryRecordKind snapshot. Pin the two enums so any drift
    # (added/renamed/reordered member) turns this test red instead of blowing
    # up at snapshot time. SearchRecordType is imported only in this test.
    assert [member.name for member in MemoryRecordKind] == [
        member.name for member in SearchRecordType
    ]
    assert [member.value for member in MemoryRecordKind] == [
        member.value for member in SearchRecordType
    ]


@pytest.mark.asyncio
async def test_create_question_is_idempotent_with_one_run_and_three_steps(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))

    first = await queue.create_question(_create_command(ACCESS_A, update_id=101))
    repeat = await queue.create_question(_create_command(ACCESS_A, update_id=101))

    assert repeat.id == first.id
    async with schema_engine.connect() as connection:
        question_count = await connection.scalar(
            select(func.count()).select_from(MemoryQuestionModel)
        )
        run_count = await connection.scalar(
            select(func.count()).select_from(MemoryAnswerRunModel)
        )
    assert question_count == 1
    assert run_count == 1

    run_id = await _run_id_for(schema_engine, first.id)
    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses == {
        MemoryStepType.RETRIEVAL: MemoryRunStatus.PENDING,
        MemoryStepType.REASONING: MemoryRunStatus.PENDING,
        MemoryStepType.DELIVERY: MemoryRunStatus.PENDING,
    }


@pytest.mark.asyncio
async def test_question_key_columns_are_not_null_and_unique(
    schema_engine: AsyncEngine,
) -> None:
    base = {
        "user_space_id": ACCESS_A.user_space_id,
        "question_text": "q",
        "current_project_id": None,
        "created_at": NOW,
        "trace_id": "a" * 32,
    }
    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(MemoryQuestionModel).values(
                    id=uuid4(), bot_id=None, telegram_update_id=5, **base
                )
            )
        await transaction.rollback()

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(MemoryQuestionModel).values(
                    id=uuid4(), bot_id=100, telegram_update_id=None, **base
                )
            )
        await transaction.rollback()

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            insert(MemoryQuestionModel).values(
                id=uuid4(), bot_id=100, telegram_update_id=7, **base
            )
        )
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(MemoryQuestionModel).values(
                    id=uuid4(), bot_id=100, telegram_update_id=7, **base
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_claim_walks_steps_only_when_predecessor_is_terminal(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=102))
    run_id = await _run_id_for(schema_engine, question.id)

    retrieval = await queue.claim_due_run(ACCESS_A, NOW, LEASE)
    assert retrieval is not None
    assert retrieval.step_type is MemoryStepType.RETRIEVAL
    assert retrieval.run_id == run_id
    assert retrieval.question_id == question.id
    assert retrieval.attempt_count == 1
    assert retrieval.lease_expires_at == NOW + LEASE
    assert retrieval.trace_id == question.trace_id

    # reasoning not due while retrieval is still RUNNING
    assert await queue.claim_due_run(ACCESS_A, NOW, LEASE) is None

    await queue.succeed_step(SucceedMemoryStepCommand(ACCESS_A, retrieval.step_id, NOW))

    reasoning = await queue.claim_due_run(ACCESS_A, NOW, LEASE)
    assert reasoning is not None
    assert reasoning.step_type is MemoryStepType.REASONING

    # delivery not due while reasoning is PENDING/RUNNING
    assert await queue.claim_due_run(ACCESS_A, NOW, LEASE) is None

    await queue.succeed_step(SucceedMemoryStepCommand(ACCESS_A, reasoning.step_id, NOW))
    delivery = await queue.claim_due_run(ACCESS_A, NOW, LEASE)
    assert delivery is not None
    assert delivery.step_type is MemoryStepType.DELIVERY


@pytest.mark.asyncio
async def test_delivery_becomes_due_when_reasoning_fails(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=103))
    run_id = await _run_id_for(schema_engine, question.id)

    retrieval = await queue.claim_due_run(ACCESS_A, NOW, LEASE)
    assert retrieval is not None
    await queue.succeed_step(SucceedMemoryStepCommand(ACCESS_A, retrieval.step_id, NOW))

    # Drive reasoning to terminal FAILED across the bounded attempts.
    at = NOW
    for _ in range(3):
        claim = await queue.claim_due_run(ACCESS_A, at, LEASE)
        assert claim is not None
        assert claim.step_type is MemoryStepType.REASONING
        outcome = await queue.fail_step(
            FailMemoryStepCommand(ACCESS_A, claim.step_id, at, "reasoning_unavailable")
        )
        at = (outcome.next_attempt_at or at) + timedelta(seconds=1)

    statuses = await _step_statuses(schema_engine, run_id)
    assert statuses[MemoryStepType.REASONING] is MemoryRunStatus.FAILED
    assert statuses[MemoryStepType.RETRIEVAL] is MemoryRunStatus.SUCCEEDED
    assert statuses[MemoryStepType.DELIVERY] is MemoryRunStatus.PENDING

    delivery = await queue.claim_due_run(ACCESS_A, at, LEASE)
    assert delivery is not None
    assert delivery.step_type is MemoryStepType.DELIVERY

    state = await queue.read_reasoning_state(ACCESS_A, run_id)
    assert state is not None
    assert state.status is MemoryRunStatus.FAILED
    assert state.has_answer is False


@pytest.mark.asyncio
async def test_reclaims_only_after_lease_expiry_on_its_own_step(
    engine: AsyncEngine,
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    await queue.create_question(_create_command(ACCESS_A, update_id=104))

    first = await queue.claim_due_run(ACCESS_A, NOW, LEASE)
    assert first is not None
    assert await queue.claim_due_run(ACCESS_A, NOW + LEASE / 2, LEASE) is None

    reclaimed = await queue.claim_due_run(ACCESS_A, NOW + LEASE, LEASE)
    assert reclaimed is not None
    assert reclaimed.step_id == first.step_id
    assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_succeed_and_fail_require_a_running_step(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=105))
    run_id = await _run_id_for(schema_engine, question.id)
    pending_retrieval = await _step_id(schema_engine, run_id, MemoryStepType.RETRIEVAL)

    with pytest.raises(ValueError):
        await queue.succeed_step(
            SucceedMemoryStepCommand(ACCESS_A, pending_retrieval, NOW)
        )
    with pytest.raises(ValueError):
        await queue.fail_step(
            FailMemoryStepCommand(ACCESS_A, pending_retrieval, NOW, "nope")
        )


@pytest.mark.asyncio
async def test_snapshot_evidence_is_idempotent_by_run(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=106))
    run_id = await _run_id_for(schema_engine, question.id)

    snippets = (
        EvidenceSnippet(
            label="S1",
            record_kind=MemoryRecordKind.DECISION,
            record_id=uuid4(),
            source_capture_event_id=uuid4(),
            created_at=NOW,
            text="решил перейти на pgvector",
        ),
        EvidenceSnippet(
            label="S2",
            record_kind=MemoryRecordKind.NOTE,
            record_id=uuid4(),
            source_capture_event_id=uuid4(),
            created_at=NOW,
            text="встреча в четверг",
        ),
    )
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, snippets))
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, snippets))

    read = await queue.read_evidence_snapshot(ACCESS_A, run_id)
    assert [snippet.label for snippet in read] == ["S1", "S2"]
    assert read[0].record_kind is MemoryRecordKind.DECISION
    assert read[0].text == "решил перейти на pgvector"


class _CitingReasoner:
    """Records the request it receives and cites one fixed label."""

    def __init__(self, cited_label: str) -> None:
        self._cited_label = cited_label
        self.requests: list[ReasoningRequest] = []

    async def reason(self, request: ReasoningRequest) -> ReasoningDraft:
        self.requests.append(request)
        return ReasoningDraft(
            model_name="nvidia/nemotron",
            prompt_version="grounded-answer-v1",
            schema_version="grounded-answer-v1",
            evidence_level=EvidenceLevel.DIRECT,
            answer="сводный вывод",
            source_labels=(self._cited_label,),
        )


def _labelled_snippet(index: int) -> EvidenceSnippet:
    return EvidenceSnippet(
        label=f"S{index}",
        record_kind=MemoryRecordKind.NOTE,
        record_id=uuid4(),
        source_capture_event_id=uuid4(),
        created_at=NOW,
        text=f"text-S{index}",
    )


@pytest.mark.asyncio
async def test_snapshot_is_read_in_numeric_label_order(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=120))
    run_id = await _run_id_for(schema_engine, question.id)

    snippets = tuple(_labelled_snippet(index) for index in range(1, 13))
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, snippets))

    read = await queue.read_evidence_snapshot(ACCESS_A, run_id)
    # Numeric order S1..S12, NOT the lexicographic S1, S10, S11, S12, S2, ...
    assert [snippet.label for snippet in read] == [f"S{i}" for i in range(1, 13)]
    assert [snippet.text for snippet in read] == [f"text-S{i}" for i in range(1, 13)]


@pytest.mark.asyncio
async def test_snapshot_rejects_non_numeric_label(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=122))
    run_id = await _run_id_for(schema_engine, question.id)

    crooked = EvidenceSnippet(
        label="X1",  # not S<digits> -> numeric sort cast would blow up in Postgres
        record_kind=MemoryRecordKind.NOTE,
        record_id=uuid4(),
        source_capture_event_id=uuid4(),
        created_at=NOW,
        text="кривая метка",
    )
    with pytest.raises(IntegrityError):
        await queue.snapshot_evidence(
            SnapshotEvidenceCommand(ACCESS_A, run_id, (crooked,))
        )


@pytest.mark.asyncio
async def test_snapshot_accepts_legal_numeric_labels(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=123))
    run_id = await _run_id_for(schema_engine, question.id)

    snippets = tuple(_labelled_snippet(index) for index in range(1, 13))
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, snippets))

    read = await queue.read_evidence_snapshot(ACCESS_A, run_id)
    assert [snippet.label for snippet in read] == [f"S{i}" for i in range(1, 13)]


@pytest.mark.asyncio
async def test_answer_source_label_stays_bound_to_its_own_snapshot_row(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=121))
    run_id = await _run_id_for(schema_engine, question.id)

    snippets = tuple(_labelled_snippet(index) for index in range(1, 13))
    record_id_by_label = {snippet.label: snippet.record_id for snippet in snippets}
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, snippets))

    snapshot = await queue.read_evidence_snapshot(ACCESS_A, run_id)
    reasoner = _CitingReasoner("S10")
    answer = await AnswerMemoryQuestion(reasoner).execute(
        question.question_text, snapshot
    )

    # The model must have seen text-S10 under label S10 (no relabelling drift).
    request = reasoner.requests[0]
    shown = {snippet.label: snippet.text for snippet in request.snippets}
    assert shown["S10"] == "text-S10"

    await queue.save_answer(
        SaveMemoryAnswerCommand(
            access_context=ACCESS_A,
            run_id=run_id,
            answer=answer,
            created_at=NOW,
            trace_id="b" * 32,
        )
    )
    read = await queue.read_answer(ACCESS_A, run_id)
    assert read is not None
    assert [source.label for source in read.sources] == ["S10"]
    # Provenance must point at S10's own evidence row, never S2's (Codex repro).
    assert read.sources[0].record_id == record_id_by_label["S10"]


@pytest.mark.asyncio
async def test_empty_snapshot_is_allowed(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=107))
    run_id = await _run_id_for(schema_engine, question.id)

    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, ()))
    assert await queue.read_evidence_snapshot(ACCESS_A, run_id) == ()


@pytest.mark.asyncio
async def test_save_answer_is_idempotent_and_binds_sources_to_snapshot(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=108))
    run_id = await _run_id_for(schema_engine, question.id)

    record_id = uuid4()
    capture_id = uuid4()
    snippet = EvidenceSnippet(
        label="S1",
        record_kind=MemoryRecordKind.DECISION,
        record_id=record_id,
        source_capture_event_id=capture_id,
        created_at=NOW,
        text="решил перейти на pgvector",
    )
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, (snippet,)))

    answer = MemoryAnswer(
        evidence_level=EvidenceLevel.DIRECT,
        answer_text="ты решил перейти на pgvector",
        sources=(
            AnswerSource(
                label="S1",
                record_kind=MemoryRecordKind.DECISION,
                record_id=record_id,
                source_capture_event_id=capture_id,
                created_at=NOW,
            ),
        ),
        model_name="nvidia/nemotron",
        prompt_version="grounded-answer-v1",
        schema_version="grounded-answer-v1",
    )
    command = SaveMemoryAnswerCommand(
        access_context=ACCESS_A,
        run_id=run_id,
        answer=answer,
        created_at=NOW,
        trace_id="b" * 32,
    )
    await queue.save_answer(command)
    await queue.save_answer(command)

    async with schema_engine.connect() as connection:
        answer_count = await connection.scalar(
            select(func.count()).select_from(MemoryAnswerModel)
        )
        source_count = await connection.scalar(
            select(func.count()).select_from(MemoryAnswerSourceModel)
        )
    assert answer_count == 1
    assert source_count == 1

    read = await queue.read_answer(ACCESS_A, run_id)
    assert read is not None
    assert read.evidence_level is EvidenceLevel.DIRECT
    assert read.model_name == "nvidia/nemotron"
    assert [source.label for source in read.sources] == ["S1"]
    assert read.sources[0].record_id == record_id


@pytest.mark.asyncio
async def test_repeat_save_answer_never_appends_new_sources(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=113))
    run_id = await _run_id_for(schema_engine, question.id)

    snippets = tuple(_labelled_snippet(index) for index in range(1, 4))
    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, snippets))

    def _source(label: str) -> AnswerSource:
        snippet = next(item for item in snippets if item.label == label)
        return AnswerSource(
            label=label,
            record_kind=snippet.record_kind,
            record_id=snippet.record_id,
            source_capture_event_id=snippet.source_capture_event_id,
            created_at=snippet.created_at,
        )

    def _answer(text: str, labels: tuple[str, ...]) -> MemoryAnswer:
        return MemoryAnswer(
            evidence_level=EvidenceLevel.DIRECT,
            answer_text=text,
            sources=tuple(_source(label) for label in labels),
            model_name="nvidia/nemotron",
            prompt_version="grounded-answer-v1",
            schema_version="grounded-answer-v1",
        )

    await queue.save_answer(
        SaveMemoryAnswerCommand(
            access_context=ACCESS_A,
            run_id=run_id,
            answer=_answer("первый ответ", ("S1", "S2")),
            created_at=NOW,
            trace_id="b" * 32,
        )
    )
    # A retry with a DIFFERENT answer and different labels must be a no-op: the
    # first answer already won, so its sources must not gain an S3 row.
    await queue.save_answer(
        SaveMemoryAnswerCommand(
            access_context=ACCESS_A,
            run_id=run_id,
            answer=_answer("второй ответ", ("S3",)),
            created_at=NOW,
            trace_id="c" * 32,
        )
    )

    async with schema_engine.connect() as connection:
        source_count = await connection.scalar(
            select(func.count()).select_from(MemoryAnswerSourceModel)
        )
    assert source_count == 2

    read = await queue.read_answer(ACCESS_A, run_id)
    assert read is not None
    assert read.answer_text == "первый ответ"
    assert [source.label for source in read.sources] == ["S1", "S2"]


@pytest.mark.asyncio
async def test_insufficient_answer_has_no_sources(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=109))
    run_id = await _run_id_for(schema_engine, question.id)

    await queue.snapshot_evidence(SnapshotEvidenceCommand(ACCESS_A, run_id, ()))
    await queue.save_answer(
        SaveMemoryAnswerCommand(
            access_context=ACCESS_A,
            run_id=run_id,
            answer=MemoryAnswer(
                evidence_level=EvidenceLevel.INSUFFICIENT,
                answer_text="недостаточно памяти",
                sources=(),
                model_name=None,
                prompt_version=None,
                schema_version=None,
            ),
            created_at=NOW,
            trace_id="c" * 32,
        )
    )
    read = await queue.read_answer(ACCESS_A, run_id)
    assert read is not None
    assert read.evidence_level is EvidenceLevel.INSUFFICIENT
    assert read.sources == ()


@pytest.mark.asyncio
async def test_answer_source_requires_a_snapshot_row(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=110))
    run_id = await _run_id_for(schema_engine, question.id)
    # snapshot has no S9 row; a source pointing at it must violate the FK.
    await queue.save_answer(
        SaveMemoryAnswerCommand(
            access_context=ACCESS_A,
            run_id=run_id,
            answer=MemoryAnswer(
                evidence_level=EvidenceLevel.INSUFFICIENT,
                answer_text="placeholder",
                sources=(),
                model_name=None,
                prompt_version=None,
                schema_version=None,
            ),
            created_at=NOW,
            trace_id="d" * 32,
        )
    )
    async with schema_engine.connect() as connection:
        answer_id = await connection.scalar(
            select(MemoryAnswerModel.id).where(MemoryAnswerModel.run_id == run_id)
        )
    assert answer_id is not None

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(MemoryAnswerSourceModel).values(
                    id=uuid4(),
                    user_space_id=ACCESS_A.user_space_id,
                    run_id=run_id,
                    answer_id=answer_id,
                    label="S9",
                    record_kind="decision",
                    record_id=uuid4(),
                    source_capture_event_id=uuid4(),
                    record_created_at=NOW,
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_overall_status_is_the_minimum_of_steps(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question = await queue.create_question(_create_command(ACCESS_A, update_id=111))
    run_id = await _run_id_for(schema_engine, question.id)

    retrieval = await queue.claim_due_run(ACCESS_A, NOW, LEASE)
    assert retrieval is not None
    await queue.succeed_step(SucceedMemoryStepCommand(ACCESS_A, retrieval.step_id, NOW))

    statuses = await _step_statuses(schema_engine, run_id)
    overall = overall_status(tuple(statuses.values()))
    # retrieval SUCCEEDED(4), reasoning/delivery PENDING(3) -> min is PENDING
    assert overall is MemoryRunStatus.PENDING


@pytest.mark.asyncio
async def test_cross_space_run_and_question_are_rejected(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    queue = PostgresMemoryQueue(create_session_factory(engine))
    question_a = await queue.create_question(_create_command(ACCESS_A, update_id=112))

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                insert(MemoryAnswerRunModel).values(
                    id=uuid4(),
                    user_space_id=ACCESS_B.user_space_id,
                    question_id=question_a.id,
                    created_at=NOW,
                    trace_id="e" * 32,
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_memory_tables_have_forced_rls_and_scoped_privileges(
    engine: AsyncEngine, isolated_database: IsolatedDatabase
) -> None:
    expected_privileges = {
        "pending_memory_questions": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "memory_questions": {"SELECT", "INSERT"},
        "memory_answer_runs": {"SELECT", "INSERT"},
        "memory_answer_steps": {"SELECT", "INSERT", "UPDATE"},
        "memory_run_evidence": {"SELECT", "INSERT"},
        "memory_answers": {"SELECT", "INSERT"},
        "memory_answer_sources": {"SELECT", "INSERT"},
    }
    async with create_session_factory(engine)() as session:
        for table_name, granted in expected_privileges.items():
            qualified_table = f'"{isolated_database.schema}"."{table_name}"'
            flags = (
                await session.execute(
                    text(
                        "SELECT c.relrowsecurity, c.relforcerowsecurity "
                        "FROM pg_class c WHERE c.oid = to_regclass(:table_name)"
                    ),
                    {"table_name": qualified_table},
                )
            ).one()
            assert flags == (True, True), table_name

            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                has_privilege = await session.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :table_name, "
                        ":privilege)"
                    ),
                    {"table_name": qualified_table, "privilege": privilege},
                )
                assert has_privilege is (privilege in granted), (table_name, privilege)

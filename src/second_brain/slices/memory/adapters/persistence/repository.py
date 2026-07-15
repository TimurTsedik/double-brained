from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    Integer,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.adapters.persistence.models import (
    MemoryAnswerModel,
    MemoryAnswerRunModel,
    MemoryAnswerSourceModel,
    MemoryAnswerStepModel,
    MemoryQuestionModel,
    MemoryRunEvidenceModel,
    PendingMemoryQuestionModel,
)
from second_brain.slices.memory.application.contracts import SetAwaitingMemoryCommand
from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    EvidenceSnippet,
    MemoryAnswer,
    MemoryAnswerStep,
    MemoryQuestion,
    MemoryReasoningState,
    MemoryRecordKind,
    MemoryRunClaim,
    MemoryRunStatus,
    MemoryStepType,
)
from second_brain.slices.memory.ports.repositories import (
    CreateMemoryQuestionCommand,
    FailMemoryStepCommand,
    SaveMemoryAnswerCommand,
    SnapshotEvidenceCommand,
    SucceedMemoryStepCommand,
)

MAX_ATTEMPTS = 3
FIRST_RETRY_DELAY = timedelta(minutes=1)
SECOND_RETRY_DELAY = timedelta(minutes=5)
_RUN_STEP_TYPES = (
    MemoryStepType.RETRIEVAL,
    MemoryStepType.REASONING,
    MemoryStepType.DELIVERY,
)
_STEP_ORDER = {
    MemoryStepType.RETRIEVAL: 0,
    MemoryStepType.REASONING: 1,
    MemoryStepType.DELIVERY: 2,
}
_TERMINAL_STATUSES = (
    MemoryRunStatus.SUCCEEDED.value,
    MemoryRunStatus.FAILED.value,
)


class PostgresMemoryQueue:
    """Owns memory state in its own short transaction per call (prod entry)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_question(
        self, command: CreateMemoryQuestionCommand
    ) -> MemoryQuestion:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).create_question(command)

    async def claim_due_run(
        self, access_context: AccessContext, now: datetime, lease_duration: timedelta
    ) -> MemoryRunClaim | None:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).claim_due_run(
                access_context, now, lease_duration
            )

    async def read_run_question(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryQuestion | None:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).read_run_question(
                access_context, run_id
            )

    async def snapshot_evidence(self, command: SnapshotEvidenceCommand) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresMemoryWriter(session).snapshot_evidence(command)

    async def read_evidence_snapshot(
        self, access_context: AccessContext, run_id: UUID
    ) -> tuple[EvidenceSnippet, ...]:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).read_evidence_snapshot(
                access_context, run_id
            )

    async def save_answer(self, command: SaveMemoryAnswerCommand) -> None:
        async with self._session_factory() as session, session.begin():
            await PostgresMemoryWriter(session).save_answer(command)

    async def read_answer(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryAnswer | None:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).read_answer(
                access_context, run_id
            )

    async def read_reasoning_state(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryReasoningState | None:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).read_reasoning_state(
                access_context, run_id
            )

    async def succeed_step(self, command: SucceedMemoryStepCommand) -> MemoryAnswerStep:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).succeed_step(command)

    async def fail_step(self, command: FailMemoryStepCommand) -> MemoryAnswerStep:
        async with self._session_factory() as session, session.begin():
            return await PostgresMemoryWriter(session).fail_step(command)


class PostgresMemoryWriter:
    """Owns memory state in a caller-controlled transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_awaiting(self, command: SetAwaitingMemoryCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        statement = (
            insert(PendingMemoryQuestionModel)
            .values(
                user_space_id=command.access_context.user_space_id,
                updated_at=command.updated_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_update(
                index_elements=[PendingMemoryQuestionModel.user_space_id],
                set_={
                    "updated_at": command.updated_at,
                    "trace_id": command.trace_id,
                },
            )
        )
        await self._session.execute(statement)

    async def cancel(self, access_context: AccessContext) -> None:
        await _set_user_space_scope(self._session, access_context)
        await self._session.execute(
            delete(PendingMemoryQuestionModel).where(
                PendingMemoryQuestionModel.user_space_id == access_context.user_space_id
            )
        )

    async def lock_pending(self, access_context: AccessContext) -> bool:
        await _set_user_space_scope(self._session, access_context)
        pending_id = await self._session.scalar(
            select(PendingMemoryQuestionModel.user_space_id)
            .where(
                PendingMemoryQuestionModel.user_space_id == access_context.user_space_id
            )
            .with_for_update()
        )
        return pending_id is not None

    async def create_question(
        self, command: CreateMemoryQuestionCommand
    ) -> MemoryQuestion:
        access = command.access_context
        await _set_user_space_scope(self._session, access)
        await self._session.execute(
            insert(MemoryQuestionModel)
            .values(
                id=uuid4(),
                user_space_id=access.user_space_id,
                bot_id=command.bot_id,
                telegram_update_id=command.telegram_update_id,
                question_text=command.question_text,
                current_project_id=command.current_project_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_nothing(
                index_elements=["user_space_id", "bot_id", "telegram_update_id"]
            )
        )
        question = await self._session.scalar(
            select(MemoryQuestionModel).where(
                MemoryQuestionModel.user_space_id == access.user_space_id,
                MemoryQuestionModel.bot_id == command.bot_id,
                MemoryQuestionModel.telegram_update_id == command.telegram_update_id,
            )
        )
        if question is None:
            raise LookupError("memory question was not persisted")

        run_id = uuid4()
        await self._session.execute(
            insert(MemoryAnswerRunModel)
            .values(
                id=run_id,
                user_space_id=access.user_space_id,
                question_id=question.id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_nothing(index_elements=["user_space_id", "question_id"])
        )
        run = await self._session.scalar(
            select(MemoryAnswerRunModel).where(
                MemoryAnswerRunModel.user_space_id == access.user_space_id,
                MemoryAnswerRunModel.question_id == question.id,
            )
        )
        if run is None:
            raise LookupError("memory answer run was not persisted")

        await self._session.execute(
            insert(MemoryAnswerStepModel)
            .values(
                [
                    {
                        "id": uuid4(),
                        "user_space_id": access.user_space_id,
                        "run_id": run.id,
                        "step_type": step_type,
                        "status": MemoryRunStatus.PENDING.value,
                        "attempt_count": 0,
                        "next_attempt_at": command.created_at,
                        "lease_expires_at": None,
                        "safe_error_code": None,
                        "started_at": None,
                        "completed_at": None,
                        "created_at": command.created_at,
                        "updated_at": command.created_at,
                    }
                    for step_type in _RUN_STEP_TYPES
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["user_space_id", "run_id", "step_type"]
            )
        )
        await self._session.flush()
        return _to_question(question)

    async def claim_due_run(
        self, access_context: AccessContext, now: datetime, lease_duration: timedelta
    ) -> MemoryRunClaim | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        await _set_user_space_scope(self._session, access_context)
        await self._finalize_exhausted_leases(access_context, now)

        step = MemoryAnswerStepModel
        predecessor = _predecessor_terminal(access_context, step)
        due = or_(
            and_(
                step.status == MemoryRunStatus.PENDING.value,
                step.next_attempt_at.is_not(None),
                step.next_attempt_at <= now,
            ),
            and_(
                step.status == MemoryRunStatus.RUNNING.value,
                step.lease_expires_at.is_not(None),
                step.lease_expires_at <= now,
            ),
        )
        statement = (
            select(MemoryAnswerStepModel, MemoryAnswerRunModel)
            .join(
                MemoryAnswerRunModel,
                and_(
                    MemoryAnswerRunModel.id == step.run_id,
                    MemoryAnswerRunModel.user_space_id == step.user_space_id,
                ),
            )
            .where(
                step.user_space_id == access_context.user_space_id,
                MemoryAnswerRunModel.user_space_id == access_context.user_space_id,
                step.attempt_count < MAX_ATTEMPTS,
                due,
                predecessor,
            )
            .order_by(
                case(
                    (step.step_type == MemoryStepType.RETRIEVAL, 0),
                    (step.step_type == MemoryStepType.REASONING, 1),
                    else_=2,
                ),
                step.created_at,
                step.id,
            )
            .with_for_update(of=MemoryAnswerStepModel, skip_locked=True)
            .limit(1)
        )
        row = (await self._session.execute(statement)).first()
        if row is None:
            return None

        claimed, run = row
        claimed.status = MemoryRunStatus.RUNNING.value
        claimed.attempt_count += 1
        claimed.next_attempt_at = None
        claimed.lease_expires_at = now + lease_duration
        claimed.safe_error_code = None
        claimed.started_at = now
        claimed.completed_at = None
        claimed.updated_at = now
        await self._session.flush()
        return MemoryRunClaim(
            step_id=claimed.id,
            run_id=run.id,
            question_id=run.question_id,
            step_type=claimed.step_type,
            attempt_count=claimed.attempt_count,
            lease_expires_at=claimed.lease_expires_at,
            trace_id=run.trace_id,
        )

    async def read_run_question(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryQuestion | None:
        await _set_user_space_scope(self._session, access_context)
        question = await self._session.scalar(
            select(MemoryQuestionModel)
            .join(
                MemoryAnswerRunModel,
                and_(
                    MemoryAnswerRunModel.question_id == MemoryQuestionModel.id,
                    MemoryAnswerRunModel.user_space_id
                    == MemoryQuestionModel.user_space_id,
                ),
            )
            .where(
                MemoryAnswerRunModel.id == run_id,
                MemoryQuestionModel.user_space_id == access_context.user_space_id,
            )
        )
        return None if question is None else _to_question(question)

    async def snapshot_evidence(self, command: SnapshotEvidenceCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        if not command.snippets:
            return
        await self._session.execute(
            insert(MemoryRunEvidenceModel)
            .values(
                [
                    {
                        "id": uuid4(),
                        "user_space_id": command.access_context.user_space_id,
                        "run_id": command.run_id,
                        "label": snippet.label,
                        "record_kind": snippet.record_kind,
                        "record_id": snippet.record_id,
                        "source_capture_event_id": snippet.source_capture_event_id,
                        "record_created_at": snippet.created_at,
                        "snippet_text": snippet.text,
                    }
                    for snippet in command.snippets
                ]
            )
            .on_conflict_do_nothing(index_elements=["user_space_id", "run_id", "label"])
        )
        await self._session.flush()

    async def read_evidence_snapshot(
        self, access_context: AccessContext, run_id: UUID
    ) -> tuple[EvidenceSnippet, ...]:
        await _set_user_space_scope(self._session, access_context)
        rows = await self._session.scalars(
            select(MemoryRunEvidenceModel)
            .where(
                MemoryRunEvidenceModel.user_space_id == access_context.user_space_id,
                MemoryRunEvidenceModel.run_id == run_id,
            )
            # Order by the numeric part of the label so S10 follows S9, not S1.
            # A plain string sort would read S10 before S2 and scramble which
            # evidence sits under which label downstream.
            .order_by(cast(func.substring(MemoryRunEvidenceModel.label, 2), Integer))
        )
        return tuple(
            EvidenceSnippet(
                label=row.label,
                record_kind=MemoryRecordKind(row.record_kind),
                record_id=row.record_id,
                source_capture_event_id=row.source_capture_event_id,
                created_at=row.record_created_at,
                text=row.snippet_text,
            )
            for row in rows
        )

    async def save_answer(self, command: SaveMemoryAnswerCommand) -> None:
        access = command.access_context
        await _set_user_space_scope(self._session, access)
        answer_id = uuid4()
        # RETURNING is empty when the row already existed (ON CONFLICT DO NOTHING).
        # Bind sources ONLY on the real first insert: a later save_answer for the
        # same run must be a strict no-op, never appending sources from a different
        # label set onto the answer that already won.
        inserted_answer_id = await self._session.scalar(
            insert(MemoryAnswerModel)
            .values(
                id=answer_id,
                user_space_id=access.user_space_id,
                run_id=command.run_id,
                evidence_level=command.answer.evidence_level,
                answer_text=command.answer.answer_text,
                model_name=command.answer.model_name,
                prompt_version=command.answer.prompt_version,
                schema_version=command.answer.schema_version,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
            .on_conflict_do_nothing(index_elements=["user_space_id", "run_id"])
            .returning(MemoryAnswerModel.id)
        )
        if inserted_answer_id is not None and command.answer.sources:
            await self._session.execute(
                insert(MemoryAnswerSourceModel).values(
                    [
                        {
                            "id": uuid4(),
                            "user_space_id": access.user_space_id,
                            "run_id": command.run_id,
                            "answer_id": inserted_answer_id,
                            "label": source.label,
                            "record_kind": source.record_kind,
                            "record_id": source.record_id,
                            "source_capture_event_id": source.source_capture_event_id,
                            "record_created_at": source.created_at,
                        }
                        for source in command.answer.sources
                    ]
                )
            )
        await self._session.flush()

    async def read_answer(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryAnswer | None:
        await _set_user_space_scope(self._session, access_context)
        answer = await self._session.scalar(
            select(MemoryAnswerModel).where(
                MemoryAnswerModel.user_space_id == access_context.user_space_id,
                MemoryAnswerModel.run_id == run_id,
            )
        )
        if answer is None:
            return None
        sources = await self._session.scalars(
            select(MemoryAnswerSourceModel)
            .where(
                MemoryAnswerSourceModel.user_space_id == access_context.user_space_id,
                MemoryAnswerSourceModel.answer_id == answer.id,
            )
            .order_by(MemoryAnswerSourceModel.label)
        )
        return MemoryAnswer(
            evidence_level=EvidenceLevel(answer.evidence_level),
            answer_text=answer.answer_text,
            sources=tuple(
                AnswerSource(
                    label=source.label,
                    record_kind=MemoryRecordKind(source.record_kind),
                    record_id=source.record_id,
                    source_capture_event_id=source.source_capture_event_id,
                    created_at=source.record_created_at,
                )
                for source in sources
            ),
            model_name=answer.model_name,
            prompt_version=answer.prompt_version,
            schema_version=answer.schema_version,
        )

    async def read_reasoning_state(
        self, access_context: AccessContext, run_id: UUID
    ) -> MemoryReasoningState | None:
        await _set_user_space_scope(self._session, access_context)
        status = await self._session.scalar(
            select(MemoryAnswerStepModel.status).where(
                MemoryAnswerStepModel.user_space_id == access_context.user_space_id,
                MemoryAnswerStepModel.run_id == run_id,
                MemoryAnswerStepModel.step_type == MemoryStepType.REASONING,
            )
        )
        if status is None:
            return None
        has_answer = await self._session.scalar(
            select(
                exists().where(
                    MemoryAnswerModel.user_space_id == access_context.user_space_id,
                    MemoryAnswerModel.run_id == run_id,
                )
            )
        )
        return MemoryReasoningState(
            status=MemoryRunStatus(status),
            has_answer=bool(has_answer),
        )

    async def succeed_step(self, command: SucceedMemoryStepCommand) -> MemoryAnswerStep:
        step = await self._lock_step(command.access_context, command.step_id)
        if step.status == MemoryRunStatus.SUCCEEDED.value:
            return _to_step(step)
        if step.status != MemoryRunStatus.RUNNING.value:
            raise ValueError("only a running memory step can succeed")
        step.status = MemoryRunStatus.SUCCEEDED.value
        step.next_attempt_at = None
        step.lease_expires_at = None
        step.safe_error_code = None
        step.completed_at = command.completed_at
        step.updated_at = command.completed_at
        await self._session.flush()
        return _to_step(step)

    async def fail_step(self, command: FailMemoryStepCommand) -> MemoryAnswerStep:
        step = await self._lock_step(command.access_context, command.step_id)
        if step.status != MemoryRunStatus.RUNNING.value:
            raise ValueError("only a running memory step can fail")
        step.lease_expires_at = None
        step.safe_error_code = command.safe_error_code
        step.updated_at = command.failed_at
        if step.attempt_count >= MAX_ATTEMPTS:
            step.status = MemoryRunStatus.FAILED.value
            step.next_attempt_at = None
            step.completed_at = command.failed_at
        else:
            step.status = MemoryRunStatus.PENDING.value
            step.next_attempt_at = command.failed_at + _retry_delay(step.attempt_count)
            step.completed_at = None
        await self._session.flush()
        return _to_step(step)

    async def _lock_step(
        self, access_context: AccessContext, step_id: UUID
    ) -> MemoryAnswerStepModel:
        await _set_user_space_scope(self._session, access_context)
        step = await self._session.scalar(
            select(MemoryAnswerStepModel)
            .where(
                MemoryAnswerStepModel.id == step_id,
                MemoryAnswerStepModel.user_space_id == access_context.user_space_id,
            )
            .with_for_update()
        )
        if step is None:
            raise LookupError("memory step was not found")
        return step

    async def _finalize_exhausted_leases(
        self, access_context: AccessContext, now: datetime
    ) -> None:
        exhausted = tuple(
            await self._session.scalars(
                select(MemoryAnswerStepModel)
                .where(
                    MemoryAnswerStepModel.user_space_id == access_context.user_space_id,
                    MemoryAnswerStepModel.status == MemoryRunStatus.RUNNING.value,
                    MemoryAnswerStepModel.attempt_count >= MAX_ATTEMPTS,
                    MemoryAnswerStepModel.lease_expires_at.is_not(None),
                    MemoryAnswerStepModel.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for step in exhausted:
            step.status = MemoryRunStatus.FAILED.value
            step.next_attempt_at = None
            step.lease_expires_at = None
            step.safe_error_code = "lease_expired"
            step.completed_at = now
            step.updated_at = now
        if exhausted:
            await self._session.flush()


def _predecessor_terminal(
    access_context: AccessContext, step: type[MemoryAnswerStepModel]
) -> ColumnElement[bool]:
    retrieval = aliased(MemoryAnswerStepModel)
    retrieval_succeeded = exists(
        select(retrieval.id).where(
            retrieval.run_id == step.run_id,
            retrieval.user_space_id == access_context.user_space_id,
            retrieval.step_type == MemoryStepType.RETRIEVAL,
            retrieval.status == MemoryRunStatus.SUCCEEDED.value,
        )
    )
    retrieval_failed = aliased(MemoryAnswerStepModel)
    retrieval_is_failed = exists(
        select(retrieval_failed.id).where(
            retrieval_failed.run_id == step.run_id,
            retrieval_failed.user_space_id == access_context.user_space_id,
            retrieval_failed.step_type == MemoryStepType.RETRIEVAL,
            retrieval_failed.status == MemoryRunStatus.FAILED.value,
        )
    )
    reasoning = aliased(MemoryAnswerStepModel)
    reasoning_terminal = exists(
        select(reasoning.id).where(
            reasoning.run_id == step.run_id,
            reasoning.user_space_id == access_context.user_space_id,
            reasoning.step_type == MemoryStepType.REASONING,
            reasoning.status.in_(_TERMINAL_STATUSES),
        )
    )
    # Delivery must reach the user on ANY terminal upstream failure. When
    # retrieval exhausts its attempts and FAILS, reasoning can never become due
    # (it needs retrieval SUCCEEDED), so gating delivery on reasoning alone would
    # hang the run forever and the user would hear nothing. Delivery is therefore
    # due when reasoning is terminal OR retrieval itself failed; either way it
    # sends a safe failure.
    return or_(
        step.step_type == MemoryStepType.RETRIEVAL,
        and_(step.step_type == MemoryStepType.REASONING, retrieval_succeeded),
        and_(
            step.step_type == MemoryStepType.DELIVERY,
            or_(reasoning_terminal, retrieval_is_failed),
        ),
    )


def _retry_delay(attempt_count: int) -> timedelta:
    if attempt_count == 1:
        return FIRST_RETRY_DELAY
    if attempt_count == 2:
        return SECOND_RETRY_DELAY
    raise ValueError("retry delay exists only after attempt one or two")


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_question(model: MemoryQuestionModel) -> MemoryQuestion:
    return MemoryQuestion(
        id=model.id,
        user_space_id=model.user_space_id,
        bot_id=model.bot_id,
        telegram_update_id=model.telegram_update_id,
        question_text=model.question_text,
        current_project_id=model.current_project_id,
        created_at=model.created_at,
        trace_id=model.trace_id,
    )


def _to_step(model: MemoryAnswerStepModel) -> MemoryAnswerStep:
    return MemoryAnswerStep(
        id=model.id,
        step_type=model.step_type,
        status=MemoryRunStatus(model.status),
        attempt_count=model.attempt_count,
        next_attempt_at=model.next_attempt_at,
        lease_expires_at=model.lease_expires_at,
        safe_error_code=model.safe_error_code,
        started_at=model.started_at,
        completed_at=model.completed_at,
    )

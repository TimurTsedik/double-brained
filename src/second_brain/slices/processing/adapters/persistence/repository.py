from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingRunModel,
    ProcessingStepModel,
)
from second_brain.slices.processing.application.contracts import (
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingRun,
    ProcessingStep,
    ProcessingStepClaim,
    ProcessingStepStatus,
    ProcessingStepType,
)

MAX_ATTEMPTS = 3
FIRST_RETRY_DELAY = timedelta(minutes=1)
SECOND_RETRY_DELAY = timedelta(minutes=5)


class PostgresProcessingRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_voice_run(
        self, command: CreateVoiceProcessingRunCommand
    ) -> ProcessingRun:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).create_voice_run(command)

    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProcessingStepClaim | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).claim_due_step(
                    access_context, now, lease_duration
                )

    async def succeed_step(
        self, command: SucceedProcessingStepCommand
    ) -> ProcessingStep:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).succeed_step(command)

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).fail_step(command)

    async def get_run(
        self, access_context: AccessContext, run_id: UUID
    ) -> ProcessingRun | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).get_run(
                    access_context, run_id
                )

    async def count_runs(self, access_context: AccessContext) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).count_runs(
                    access_context
                )


class PostgresProcessingWriter:
    """Owns processing state in a caller-controlled transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_voice_run(
        self, command: CreateVoiceProcessingRunCommand
    ) -> ProcessingRun:
        await _set_user_space_scope(self._session, command.access_context)
        run = ProcessingRunModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            capture_event_id=command.capture_event_id,
            output_type=command.output_type,
            version=1,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )
        steps = tuple(
            ProcessingStepModel(
                id=uuid4(),
                user_space_id=command.access_context.user_space_id,
                processing_run_id=run.id,
                step_type=step_type,
                status=ProcessingStepStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=command.created_at,
                lease_expires_at=None,
                safe_error_code=None,
                started_at=None,
                completed_at=None,
                created_at=command.created_at,
                updated_at=command.created_at,
                trace_id=command.trace_id,
            )
            for step_type in ProcessingStepType
        )
        self._session.add(run)
        self._session.add_all(steps)
        await self._session.flush()
        return _to_run(run, steps)

    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProcessingStepClaim | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        await _set_user_space_scope(self._session, access_context)
        await self._finalize_exhausted_leases(access_context, now)

        download = aliased(ProcessingStepModel)
        download_succeeded = exists(
            select(download.id).where(
                download.processing_run_id == ProcessingStepModel.processing_run_id,
                download.user_space_id == access_context.user_space_id,
                download.step_type == ProcessingStepType.AUDIO_DOWNLOAD,
                download.status == ProcessingStepStatus.SUCCEEDED.value,
            )
        )
        due = or_(
            and_(
                ProcessingStepModel.status == ProcessingStepStatus.PENDING.value,
                ProcessingStepModel.next_attempt_at.is_not(None),
                ProcessingStepModel.next_attempt_at <= now,
            ),
            and_(
                ProcessingStepModel.status == ProcessingStepStatus.RUNNING.value,
                ProcessingStepModel.lease_expires_at.is_not(None),
                ProcessingStepModel.lease_expires_at <= now,
            ),
        )
        statement = (
            select(ProcessingStepModel, ProcessingRunModel)
            .join(
                ProcessingRunModel,
                and_(
                    ProcessingRunModel.id == ProcessingStepModel.processing_run_id,
                    ProcessingRunModel.user_space_id
                    == ProcessingStepModel.user_space_id,
                ),
            )
            .where(
                ProcessingStepModel.user_space_id == access_context.user_space_id,
                ProcessingRunModel.user_space_id == access_context.user_space_id,
                ProcessingStepModel.attempt_count < MAX_ATTEMPTS,
                due,
                or_(
                    ProcessingStepModel.step_type == ProcessingStepType.AUDIO_DOWNLOAD,
                    download_succeeded,
                ),
            )
            .order_by(
                case(
                    (
                        ProcessingStepModel.step_type
                        == ProcessingStepType.AUDIO_DOWNLOAD,
                        0,
                    ),
                    else_=1,
                ),
                ProcessingStepModel.created_at,
                ProcessingStepModel.id,
            )
            .with_for_update(of=ProcessingStepModel, skip_locked=True)
            .limit(1)
        )
        row = (await self._session.execute(statement)).first()
        if row is None:
            return None

        step, run = row
        step.status = ProcessingStepStatus.RUNNING.value
        step.attempt_count += 1
        step.next_attempt_at = None
        step.lease_expires_at = now + lease_duration
        step.safe_error_code = None
        step.started_at = now
        step.completed_at = None
        step.updated_at = now
        await self._session.flush()
        return ProcessingStepClaim(
            step_id=step.id,
            run_id=run.id,
            capture_event_id=run.capture_event_id,
            step_type=step.step_type,
            output_type=run.output_type,
            attempt_count=step.attempt_count,
            lease_expires_at=step.lease_expires_at,
            trace_id=run.trace_id,
        )

    async def succeed_step(
        self, command: SucceedProcessingStepCommand
    ) -> ProcessingStep:
        step = await self._lock_step(command.access_context, command.step_id)
        if step.status == ProcessingStepStatus.SUCCEEDED.value:
            return _to_step(step)
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running processing step can succeed")

        step.status = ProcessingStepStatus.SUCCEEDED.value
        step.next_attempt_at = None
        step.lease_expires_at = None
        step.safe_error_code = None
        step.completed_at = command.completed_at
        step.updated_at = command.completed_at
        await self._session.flush()
        return _to_step(step)

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep:
        step = await self._lock_step(command.access_context, command.step_id)
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running processing step can fail")

        step.lease_expires_at = None
        step.safe_error_code = command.safe_error_code
        step.updated_at = command.failed_at
        if step.attempt_count >= MAX_ATTEMPTS:
            step.status = ProcessingStepStatus.FAILED.value
            step.next_attempt_at = None
            step.completed_at = command.failed_at
            if step.step_type is ProcessingStepType.AUDIO_DOWNLOAD:
                await self._skip_transcription(
                    command.access_context,
                    step.processing_run_id,
                    command.failed_at,
                )
        else:
            step.status = ProcessingStepStatus.PENDING.value
            step.next_attempt_at = command.failed_at + _retry_delay(step.attempt_count)
            step.completed_at = None
        await self._session.flush()
        return _to_step(step)

    async def get_run(
        self, access_context: AccessContext, run_id: UUID
    ) -> ProcessingRun | None:
        await _set_user_space_scope(self._session, access_context)
        run = await self._session.scalar(
            select(ProcessingRunModel).where(
                ProcessingRunModel.id == run_id,
                ProcessingRunModel.user_space_id == access_context.user_space_id,
            )
        )
        if run is None:
            return None
        steps = tuple(
            await self._session.scalars(
                select(ProcessingStepModel)
                .where(
                    ProcessingStepModel.processing_run_id == run.id,
                    ProcessingStepModel.user_space_id == access_context.user_space_id,
                )
                .order_by(
                    case(
                        (
                            ProcessingStepModel.step_type
                            == ProcessingStepType.AUDIO_DOWNLOAD,
                            0,
                        ),
                        else_=1,
                    )
                )
            )
        )
        return _to_run(run, steps)

    async def count_runs(self, access_context: AccessContext) -> int:
        await _set_user_space_scope(self._session, access_context)
        count = await self._session.scalar(
            select(func.count())
            .select_from(ProcessingRunModel)
            .where(ProcessingRunModel.user_space_id == access_context.user_space_id)
        )
        return int(count or 0)

    async def _lock_step(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingStepModel:
        await _set_user_space_scope(self._session, access_context)
        step = await self._session.scalar(
            select(ProcessingStepModel)
            .where(
                ProcessingStepModel.id == step_id,
                ProcessingStepModel.user_space_id == access_context.user_space_id,
            )
            .with_for_update()
        )
        if step is None:
            raise LookupError("processing step was not found")
        return step

    async def _skip_transcription(
        self,
        access_context: AccessContext,
        run_id: UUID,
        completed_at: datetime,
    ) -> None:
        transcription = await self._session.scalar(
            select(ProcessingStepModel)
            .where(
                ProcessingStepModel.processing_run_id == run_id,
                ProcessingStepModel.user_space_id == access_context.user_space_id,
                ProcessingStepModel.step_type == ProcessingStepType.TRANSCRIPTION,
            )
            .with_for_update()
        )
        if (
            transcription is not None
            and transcription.status == ProcessingStepStatus.PENDING.value
        ):
            transcription.status = ProcessingStepStatus.SKIPPED.value
            transcription.next_attempt_at = None
            transcription.lease_expires_at = None
            transcription.completed_at = completed_at
            transcription.updated_at = completed_at

    async def _finalize_exhausted_leases(
        self, access_context: AccessContext, now: datetime
    ) -> None:
        exhausted = tuple(
            await self._session.scalars(
                select(ProcessingStepModel)
                .where(
                    ProcessingStepModel.user_space_id == access_context.user_space_id,
                    ProcessingStepModel.status == ProcessingStepStatus.RUNNING.value,
                    ProcessingStepModel.attempt_count >= MAX_ATTEMPTS,
                    ProcessingStepModel.lease_expires_at.is_not(None),
                    ProcessingStepModel.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for step in exhausted:
            step.status = ProcessingStepStatus.FAILED.value
            step.next_attempt_at = None
            step.lease_expires_at = None
            step.safe_error_code = "lease_expired"
            step.completed_at = now
            step.updated_at = now
            if step.step_type is ProcessingStepType.AUDIO_DOWNLOAD:
                await self._skip_transcription(
                    access_context, step.processing_run_id, now
                )
        if exhausted:
            await self._session.flush()


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


def _to_step(model: ProcessingStepModel) -> ProcessingStep:
    return ProcessingStep(
        id=model.id,
        step_type=model.step_type,
        status=ProcessingStepStatus(model.status),
        attempt_count=model.attempt_count,
        next_attempt_at=model.next_attempt_at,
        lease_expires_at=model.lease_expires_at,
        safe_error_code=model.safe_error_code,
        started_at=model.started_at,
        completed_at=model.completed_at,
    )


def _to_run(
    model: ProcessingRunModel, steps: tuple[ProcessingStepModel, ...]
) -> ProcessingRun:
    ordered_steps = sorted(
        steps,
        key=lambda step: (
            0 if step.step_type is ProcessingStepType.AUDIO_DOWNLOAD else 1
        ),
    )
    return ProcessingRun(
        id=model.id,
        user_space_id=model.user_space_id,
        capture_event_id=model.capture_event_id,
        output_type=model.output_type,
        version=model.version,
        steps=tuple(_to_step(step) for step in ordered_steps),
        trace_id=model.trace_id,
    )

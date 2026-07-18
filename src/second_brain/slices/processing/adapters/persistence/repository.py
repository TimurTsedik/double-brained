from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, exists, func, not_, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingNoticeModel,
    ProcessingRunModel,
    ProcessingStepModel,
    TranscriptModel,
)
from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    CreateImageProcessingRunCommand,
    CreateTextProcessingRunCommand,
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    MarkProcessingNoticeSentCommand,
    SkipProcessingStepCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingCompletionTarget,
    ProcessingNoticeClaim,
    ProcessingNoticeKind,
    ProcessingNoticeStatus,
    ProcessingRun,
    ProcessingStep,
    ProcessingStepClaim,
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)

MAX_ATTEMPTS = 3
FIRST_RETRY_DELAY = timedelta(minutes=1)
SECOND_RETRY_DELAY = timedelta(minutes=5)
# Детерминированные исходы: повтор даст ровно тот же результат, поэтому шаг
# падает сразу с первой попытки — без ретраев (пустая запись пуста всегда).
TERMINAL_ERROR_CODES = frozenset({"empty_transcript"})
_STEP_ORDER = {
    ProcessingStepType.AUDIO_DOWNLOAD: 0,
    ProcessingStepType.IMAGE_DOWNLOAD: 0,
    ProcessingStepType.TRANSCRIPTION: 1,
    ProcessingStepType.CLASSIFICATION: 2,
    ProcessingStepType.INDEXING: 3,
}
_CREATE_RUN_COMMAND = (
    CreateVoiceProcessingRunCommand
    | CreateTextProcessingRunCommand
    | CreateImageProcessingRunCommand
)


class PostgresProcessingRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_voice_run(
        self, command: CreateVoiceProcessingRunCommand
    ) -> ProcessingRun:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).create_voice_run(command)

    async def create_text_run(
        self, command: CreateTextProcessingRunCommand
    ) -> ProcessingRun:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).create_text_run(command)

    async def create_image_run(
        self, command: CreateImageProcessingRunCommand
    ) -> ProcessingRun:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).create_image_run(command)

    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
        step_types: tuple[ProcessingStepType, ...],
    ) -> ProcessingStepClaim | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).claim_due_step(
                    access_context, now, lease_duration, step_types
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

    async def skip_step(self, command: SkipProcessingStepCommand) -> ProcessingStep:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).skip_step(command)

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

    async def complete_voice_download(
        self, command: CompleteVoiceDownloadCommand
    ) -> ProcessingStep:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).complete_voice_download(
                    command
                )

    async def complete_image_download(
        self, command: CompleteImageDownloadCommand
    ) -> ProcessingStep:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).complete_image_download(
                    command
                )

    async def lock_transcription_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(
                    session
                ).lock_transcription_target(access_context, step_id)

    async def lock_indexing_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).lock_indexing_target(
                    access_context, step_id
                )

    async def complete_voice_transcription(
        self, command: CompleteVoiceTranscriptionCommand
    ) -> ProcessingStep:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(
                    session
                ).complete_voice_transcription(command)

    async def claim_due_notice(
        self, access_context: AccessContext, now: datetime
    ) -> ProcessingNoticeClaim | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresProcessingWriter(session).claim_due_notice(
                    access_context, now
                )

    async def mark_notice_sent(self, command: MarkProcessingNoticeSentCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresProcessingWriter(session).mark_notice_sent(command)


class PostgresProcessingWriter:
    """Owns processing state in a caller-controlled transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_voice_run(
        self, command: CreateVoiceProcessingRunCommand
    ) -> ProcessingRun:
        # Явный набор шагов голоса: НЕ tuple(ProcessingStepType) — enum вырос
        # image_download'ом, голосовому прогону он не принадлежит.
        return await self._create_run(
            command,
            (
                ProcessingStepType.AUDIO_DOWNLOAD,
                ProcessingStepType.TRANSCRIPTION,
                ProcessingStepType.CLASSIFICATION,
                ProcessingStepType.INDEXING,
            ),
        )

    async def create_text_run(
        self, command: CreateTextProcessingRunCommand
    ) -> ProcessingRun:
        return await self._create_run(
            command,
            (ProcessingStepType.CLASSIFICATION, ProcessingStepType.INDEXING),
        )

    async def create_image_run(
        self, command: CreateImageProcessingRunCommand
    ) -> ProcessingRun:
        # С подписью запись уже создана синхронно → download + classification +
        # indexing (обе НЕ гейтятся download'ом — см. claim_due_step). Без
        # подписи — source-only: единственный шаг IMAGE_DOWNLOAD, типа нет.
        if command.output_type is None:
            return await self._create_run(command, (ProcessingStepType.IMAGE_DOWNLOAD,))
        return await self._create_run(
            command,
            (
                ProcessingStepType.IMAGE_DOWNLOAD,
                ProcessingStepType.CLASSIFICATION,
                ProcessingStepType.INDEXING,
            ),
        )

    async def _create_run(
        self,
        command: _CREATE_RUN_COMMAND,
        step_types: tuple[ProcessingStepType, ...],
    ) -> ProcessingRun:
        await _set_user_space_scope(self._session, command.access_context)
        run = ProcessingRunModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            capture_event_id=command.capture_event_id,
            output_type=command.output_type,
            source_only=command.output_type is None,
            route_default_by_time=command.route_default_by_time,
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
            for step_type in step_types
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
        step_types: tuple[ProcessingStepType, ...],
    ) -> ProcessingStepClaim | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        if not step_types:
            raise ValueError("at least one processing step type must be allowed")
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
        transcription = aliased(ProcessingStepModel)
        transcription_exists = exists(
            select(transcription.id).where(
                transcription.processing_run_id
                == ProcessingStepModel.processing_run_id,
                transcription.user_space_id == access_context.user_space_id,
                transcription.step_type == ProcessingStepType.TRANSCRIPTION,
            )
        )
        transcription_succeeded = exists(
            select(transcription.id).where(
                transcription.processing_run_id
                == ProcessingStepModel.processing_run_id,
                transcription.user_space_id == access_context.user_space_id,
                transcription.step_type == ProcessingStepType.TRANSCRIPTION,
                transcription.status == ProcessingStepStatus.SUCCEEDED.value,
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
                ProcessingStepModel.step_type.in_(step_types),
                ProcessingStepModel.attempt_count < MAX_ATTEMPTS,
                due,
                or_(
                    ProcessingStepModel.step_type == ProcessingStepType.AUDIO_DOWNLOAD,
                    # Скачивание оригинала фото ничем не гейтится; classification/
                    # indexing image-прогона идут по ветке «нет TRANSCRIPTION» —
                    # подпись независима от байтов картинки.
                    ProcessingStepModel.step_type == ProcessingStepType.IMAGE_DOWNLOAD,
                    and_(
                        ProcessingStepModel.step_type
                        == ProcessingStepType.TRANSCRIPTION,
                        download_succeeded,
                    ),
                    and_(
                        ProcessingStepModel.step_type
                        == ProcessingStepType.CLASSIFICATION,
                        or_(not_(transcription_exists), transcription_succeeded),
                    ),
                    and_(
                        ProcessingStepModel.step_type == ProcessingStepType.INDEXING,
                        or_(not_(transcription_exists), transcription_succeeded),
                    ),
                ),
            )
            .order_by(
                case(
                    (
                        ProcessingStepModel.step_type
                        == ProcessingStepType.AUDIO_DOWNLOAD,
                        0,
                    ),
                    (
                        ProcessingStepModel.step_type
                        == ProcessingStepType.IMAGE_DOWNLOAD,
                        0,
                    ),
                    (
                        ProcessingStepModel.step_type
                        == ProcessingStepType.TRANSCRIPTION,
                        1,
                    ),
                    (
                        ProcessingStepModel.step_type
                        == ProcessingStepType.CLASSIFICATION,
                        2,
                    ),
                    else_=3,
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

    async def complete_voice_download(
        self, command: CompleteVoiceDownloadCommand
    ) -> ProcessingStep:
        step, run = await self._lock_step_and_run(
            command.access_context, command.step_id
        )
        if step.step_type is not ProcessingStepType.AUDIO_DOWNLOAD:
            raise ValueError("processing step is not an audio download")
        if run.capture_event_id != command.capture_event_id:
            raise ValueError("download completion source does not match its run")
        return await self._succeed_locked_step(step, command.completed_at)

    async def complete_image_download(
        self, command: CompleteImageDownloadCommand
    ) -> ProcessingStep:
        step, run = await self._lock_step_and_run(
            command.access_context, command.step_id
        )
        if step.step_type is not ProcessingStepType.IMAGE_DOWNLOAD:
            raise ValueError("processing step is not an image download")
        if run.capture_event_id != command.capture_event_id:
            raise ValueError("download completion source does not match its run")
        return await self._succeed_locked_step(step, command.completed_at)

    async def lock_transcription_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget:
        step, run = await self._lock_step_and_run(access_context, step_id)
        if step.step_type is not ProcessingStepType.TRANSCRIPTION:
            raise ValueError("processing step is not a transcription")
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running transcription can complete")
        return ProcessingCompletionTarget(
            step_id=step.id,
            run_id=run.id,
            capture_event_id=run.capture_event_id,
            output_type=_require_output_type(run),
            version=run.version,
            trace_id=run.trace_id,
            route_default_by_time=run.route_default_by_time,
        )

    async def lock_classification_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget:
        step, run = await self._lock_step_and_run(access_context, step_id)
        if step.step_type is not ProcessingStepType.CLASSIFICATION:
            raise ValueError("processing step is not a classification")
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running classification can complete")
        return ProcessingCompletionTarget(
            step_id=step.id,
            run_id=run.id,
            capture_event_id=run.capture_event_id,
            output_type=_require_output_type(run),
            version=run.version,
            trace_id=run.trace_id,
        )

    async def lock_indexing_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget:
        step, run = await self._lock_step_and_run(access_context, step_id)
        if step.step_type is not ProcessingStepType.INDEXING:
            raise ValueError("processing step is not an indexing step")
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running indexing step can complete")
        return ProcessingCompletionTarget(
            step_id=step.id,
            run_id=run.id,
            capture_event_id=run.capture_event_id,
            output_type=_require_output_type(run),
            version=run.version,
            trace_id=run.trace_id,
        )

    async def complete_voice_transcription(
        self, command: CompleteVoiceTranscriptionCommand
    ) -> ProcessingStep:
        if not command.draft.text.strip():
            raise ValueError("empty_transcript")
        step, run = await self._lock_step_and_run(
            command.access_context, command.step_id
        )
        if step.step_type is not ProcessingStepType.TRANSCRIPTION:
            raise ValueError("processing step is not a transcription")
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running transcription can complete")
        self._session.add(
            TranscriptModel(
                id=uuid4(),
                user_space_id=command.access_context.user_space_id,
                capture_event_id=run.capture_event_id,
                processing_run_id=run.id,
                version=run.version,
                text=command.draft.text,
                language=command.draft.language,
                language_probability=command.draft.language_probability,
                model_name=command.draft.model_name,
                segments=_segments_json(command),
                created_at=command.completed_at,
                trace_id=run.trace_id,
            )
        )
        await self._create_notice(
            command.access_context,
            run.id,
            ProcessingNoticeKind.SUCCESS,
            command.completed_at,
            run.trace_id,
            # Метка «сохранено: …» — по ФАКТУ материализации (голос со временем
            # мог стать задачей); прогон менять нельзя (append-only).
            output_type=command.resolved_output_type or run.output_type,
        )
        result = await self._succeed_locked_step(step, command.completed_at)
        await self._session.flush()
        return result

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep:
        step = await self._lock_step(command.access_context, command.step_id)
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running processing step can fail")

        step.lease_expires_at = None
        step.safe_error_code = command.safe_error_code
        step.updated_at = command.failed_at
        if (
            step.attempt_count >= MAX_ATTEMPTS
            or command.safe_error_code in TERMINAL_ERROR_CODES
        ):
            step.status = ProcessingStepStatus.FAILED.value
            step.next_attempt_at = None
            step.completed_at = command.failed_at
            if step.step_type in (
                ProcessingStepType.AUDIO_DOWNLOAD,
                ProcessingStepType.TRANSCRIPTION,
            ):
                await self._skip_dependents(
                    command.access_context,
                    step.processing_run_id,
                    step.step_type,
                    command.failed_at,
                )
            if step.step_type is not ProcessingStepType.INDEXING:
                await self._create_notice(
                    command.access_context,
                    step.processing_run_id,
                    # Пустая запись — честное «не расслышал», не generic-сбой.
                    ProcessingNoticeKind.EMPTY_VOICE
                    if command.safe_error_code == "empty_transcript"
                    else ProcessingNoticeKind.FAILURE,
                    command.failed_at,
                    step.trace_id,
                )
        else:
            step.status = ProcessingStepStatus.PENDING.value
            step.next_attempt_at = command.failed_at + _retry_delay(step.attempt_count)
            step.completed_at = None
        await self._session.flush()
        return _to_step(step)

    async def skip_step(self, command: SkipProcessingStepCommand) -> ProcessingStep:
        step = await self._lock_step(command.access_context, command.step_id)
        if step.status == ProcessingStepStatus.SKIPPED.value:
            return _to_step(step)
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running processing step can be skipped")
        step.status = ProcessingStepStatus.SKIPPED.value
        step.next_attempt_at = None
        step.lease_expires_at = None
        step.safe_error_code = command.safe_reason_code
        step.completed_at = command.skipped_at
        step.updated_at = command.skipped_at
        await self._session.flush()
        return _to_step(step)

    async def claim_due_notice(
        self, access_context: AccessContext, now: datetime
    ) -> ProcessingNoticeClaim | None:
        await _set_user_space_scope(self._session, access_context)
        row = (
            await self._session.execute(
                select(ProcessingNoticeModel, ProcessingRunModel)
                .join(
                    ProcessingRunModel,
                    and_(
                        ProcessingRunModel.id
                        == ProcessingNoticeModel.processing_run_id,
                        ProcessingRunModel.user_space_id
                        == ProcessingNoticeModel.user_space_id,
                    ),
                )
                .where(
                    ProcessingNoticeModel.user_space_id == access_context.user_space_id,
                    ProcessingRunModel.user_space_id == access_context.user_space_id,
                    ProcessingNoticeModel.status == ProcessingNoticeStatus.PENDING,
                    ProcessingNoticeModel.attempt_count < MAX_ATTEMPTS,
                    ProcessingNoticeModel.next_attempt_at.is_not(None),
                    ProcessingNoticeModel.next_attempt_at <= now,
                )
                .order_by(
                    ProcessingNoticeModel.created_at,
                    ProcessingNoticeModel.id,
                )
                .with_for_update(of=ProcessingNoticeModel, skip_locked=True)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        notice, run = row
        notice.attempt_count += 1
        notice.next_attempt_at = (
            now + FIRST_RETRY_DELAY if notice.attempt_count < MAX_ATTEMPTS else None
        )
        notice.updated_at = now
        await self._session.flush()
        return ProcessingNoticeClaim(
            notice_id=notice.id,
            run_id=run.id,
            kind=notice.kind,
            # Успех несёт фактический тип на самом уведомлении; у сбойных/пустых
            # он NULL — там тип не показывается, берём замороженный из прогона.
            output_type=notice.output_type or run.output_type,
            trace_id=notice.trace_id,
            attempt_count=notice.attempt_count,
        )

    async def mark_notice_sent(self, command: MarkProcessingNoticeSentCommand) -> None:
        await _set_user_space_scope(self._session, command.access_context)
        notice = await self._session.scalar(
            select(ProcessingNoticeModel)
            .where(
                ProcessingNoticeModel.id == command.notice_id,
                ProcessingNoticeModel.user_space_id
                == command.access_context.user_space_id,
            )
            .with_for_update()
        )
        if notice is None:
            raise LookupError("processing notice was not found")
        if notice.status is ProcessingNoticeStatus.SENT:
            return
        notice.status = ProcessingNoticeStatus.SENT
        notice.next_attempt_at = None
        notice.sent_at = command.sent_at
        notice.updated_at = command.sent_at
        await self._session.flush()

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
                        (
                            ProcessingStepModel.step_type
                            == ProcessingStepType.TRANSCRIPTION,
                            1,
                        ),
                        (
                            ProcessingStepModel.step_type
                            == ProcessingStepType.CLASSIFICATION,
                            2,
                        ),
                        else_=3,
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

    async def _succeed_locked_step(
        self, step: ProcessingStepModel, completed_at: datetime
    ) -> ProcessingStep:
        if step.status == ProcessingStepStatus.SUCCEEDED.value:
            return _to_step(step)
        if step.status != ProcessingStepStatus.RUNNING.value:
            raise ValueError("only a running processing step can succeed")
        step.status = ProcessingStepStatus.SUCCEEDED.value
        step.next_attempt_at = None
        step.lease_expires_at = None
        step.safe_error_code = None
        step.completed_at = completed_at
        step.updated_at = completed_at
        await self._session.flush()
        return _to_step(step)

    async def _lock_step_and_run(
        self, access_context: AccessContext, step_id: UUID
    ) -> tuple[ProcessingStepModel, ProcessingRunModel]:
        await _set_user_space_scope(self._session, access_context)
        row = (
            await self._session.execute(
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
                    ProcessingStepModel.id == step_id,
                    ProcessingStepModel.user_space_id == access_context.user_space_id,
                    ProcessingRunModel.user_space_id == access_context.user_space_id,
                )
                .with_for_update(of=ProcessingStepModel)
            )
        ).one_or_none()
        if row is None:
            raise LookupError("processing step was not found")
        return row[0], row[1]

    async def _create_notice(
        self,
        access_context: AccessContext,
        run_id: UUID,
        kind: ProcessingNoticeKind,
        created_at: datetime,
        trace_id: str,
        output_type: TranscriptionOutputType | None = None,
    ) -> None:
        await _set_user_space_scope(self._session, access_context)
        await self._session.execute(
            insert(ProcessingNoticeModel)
            .values(
                id=uuid4(),
                user_space_id=access_context.user_space_id,
                processing_run_id=run_id,
                kind=kind,
                status=ProcessingNoticeStatus.PENDING,
                output_type=output_type,
                attempt_count=0,
                next_attempt_at=created_at,
                sent_at=None,
                created_at=created_at,
                updated_at=created_at,
                trace_id=trace_id,
            )
            .on_conflict_do_nothing(constraint="uq_processing_notices_run_kind")
        )

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

    async def _skip_dependents(
        self,
        access_context: AccessContext,
        run_id: UUID,
        failed_step_type: ProcessingStepType,
        completed_at: datetime,
    ) -> None:
        dependent_types = (
            (
                ProcessingStepType.TRANSCRIPTION,
                ProcessingStepType.CLASSIFICATION,
                ProcessingStepType.INDEXING,
            )
            if failed_step_type is ProcessingStepType.AUDIO_DOWNLOAD
            else (ProcessingStepType.CLASSIFICATION, ProcessingStepType.INDEXING)
        )
        dependents = tuple(
            await self._session.scalars(
                select(ProcessingStepModel)
                .where(
                    ProcessingStepModel.processing_run_id == run_id,
                    ProcessingStepModel.user_space_id == access_context.user_space_id,
                    ProcessingStepModel.step_type.in_(dependent_types),
                )
                .with_for_update()
            )
        )
        for dependent in dependents:
            if dependent.status == ProcessingStepStatus.PENDING.value:
                dependent.status = ProcessingStepStatus.SKIPPED.value
                dependent.next_attempt_at = None
                dependent.lease_expires_at = None
                dependent.completed_at = completed_at
                dependent.updated_at = completed_at

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
            if step.step_type in (
                ProcessingStepType.AUDIO_DOWNLOAD,
                ProcessingStepType.TRANSCRIPTION,
            ):
                await self._skip_dependents(
                    access_context,
                    step.processing_run_id,
                    step.step_type,
                    now,
                )
            if step.step_type is not ProcessingStepType.INDEXING:
                await self._create_notice(
                    access_context,
                    step.processing_run_id,
                    ProcessingNoticeKind.FAILURE,
                    now,
                    step.trace_id,
                )
        if exhausted:
            await self._session.flush()


def _require_output_type(run: ProcessingRunModel) -> TranscriptionOutputType:
    # Source-only прогоны (output_type NULL) состоят из одного download-шага —
    # transcription/classification/indexing у них не существует, поэтому сюда
    # такой прогон попасть не может; страховка на случай порчи данных.
    if run.output_type is None:
        raise ValueError("source-only processing run has no output type")
    return run.output_type


def _retry_delay(attempt_count: int) -> timedelta:
    if attempt_count == 1:
        return FIRST_RETRY_DELAY
    if attempt_count == 2:
        return SECOND_RETRY_DELAY
    raise ValueError("retry delay exists only after attempt one or two")


def _segments_json(command: CompleteVoiceTranscriptionCommand) -> list[object]:
    return [
        {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "words": [
                {
                    "start": word.start,
                    "end": word.end,
                    "text": word.text,
                }
                for word in segment.words
            ],
        }
        for segment in command.draft.segments
    ]


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
        key=lambda step: _STEP_ORDER[step.step_type],
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

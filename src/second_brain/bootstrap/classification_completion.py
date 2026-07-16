from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.bootstrap.task_capture_in_transaction import (
    build_task_capture,
    send_reminder_confirmations,
)
from second_brain.slices.classification.adapters.persistence.repository import (
    PostgresClassificationWriter,
)
from second_brain.slices.classification.application.contracts import (
    CompleteClassificationCommand,
    StoreClassificationResultCommand,
)
from second_brain.slices.classification.domain.entities import (
    CandidateDisposition,
    CandidateStorageStatus,
    GroundedCandidate,
    StoredCandidate,
)
from second_brain.slices.identity.application.contracts import WorkerIdentityPort
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    SucceedProcessingStepCommand,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkWriter,
)
from second_brain.slices.projects.application.contracts import (
    InheritCaptureProjectLinksCommand,
)
from second_brain.slices.projects.domain.entities import ProjectContentKind
from second_brain.slices.reminders.application.contracts import ReminderDeliveryPort
from second_brain.slices.tasks.application.contracts import CreateTypedCaptureCommand
from second_brain.slices.tasks.domain.entities import PendingCaptureType


class ClassificationCompletionInTransaction:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        delivery_port: ReminderDeliveryPort,
        identity: WorkerIdentityPort,
    ) -> None:
        self._session_factory = session_factory
        self._delivery_port = delivery_port
        self._identity = identity

    async def complete(self, command: CompleteClassificationCommand) -> None:
        outcome = command.outcome
        if outcome.skipped_reason is not None:
            raise ValueError("a skipped classification cannot be completed")
        if (
            outcome.model_name is None
            or outcome.prompt_version is None
            or outcome.schema_version is None
        ):
            raise ValueError("completed classification metadata is required")

        # (remind_at UTC, tz пространства) созданных здесь напоминаний — для
        # подтверждения «⏰ Напомню…» после коммита.
        confirmations: list[tuple[datetime, str]] = []
        async with self._session_factory() as session:
            async with session.begin():
                processing = PostgresProcessingWriter(session)
                target = await processing.lock_classification_target(
                    command.access_context, command.step_id
                )
                candidates = []
                for candidate in outcome.candidates:
                    record_id = await _materialize_candidate(
                        session,
                        command,
                        target.capture_event_id,
                        target.trace_id,
                        candidate,
                        lambda remind_at, tz: confirmations.append((remind_at, tz)),
                    )
                    candidates.append(_stored_candidate(candidate, record_id))

                await PostgresClassificationWriter(session).create(
                    StoreClassificationResultCommand(
                        access_context=command.access_context,
                        processing_run_id=target.run_id,
                        capture_event_id=target.capture_event_id,
                        source_sha256=outcome.source_sha256,
                        model_name=outcome.model_name,
                        prompt_version=outcome.prompt_version,
                        schema_version=outcome.schema_version,
                        candidates=tuple(candidates),
                        discarded_candidate_count=outcome.discarded_candidate_count,
                        created_at=command.completed_at,
                        trace_id=target.trace_id,
                    )
                )
                await processing.succeed_step(
                    SucceedProcessingStepCommand(
                        access_context=command.access_context,
                        step_id=command.step_id,
                        completed_at=command.completed_at,
                    )
                )
        # После коммита: авто-созданная задача со временем молчала бы иначе —
        # владелец не знал бы, что будильник заведён (кнопочный путь подтверждает
        # poller-ack'ом, здесь отвечает воркер). Осознанный lean-край: сбой между
        # коммитом и отправкой теряет/дублирует ТОЛЬКО подтверждение.
        await send_reminder_confirmations(
            self._delivery_port, self._identity, command.access_context, confirmations
        )


async def _materialize_candidate(
    session: AsyncSession,
    command: CompleteClassificationCommand,
    capture_event_id: UUID,
    trace_id: str,
    candidate: GroundedCandidate,
    on_reminder_created: Callable[[datetime, str], None],
) -> UUID | None:
    if candidate.disposition is not CandidateDisposition.MATERIALIZE:
        return None
    task_capture = build_task_capture(session, on_reminder_created)
    record = await task_capture.create_for_selection(
        CreateTypedCaptureCommand(
            access_context=command.access_context,
            selection=PendingCaptureType(candidate.candidate_type.value),
            text=candidate.source_quote,
            source_capture_event_id=capture_event_id,
            created_at=command.completed_at,
            trace_id=trace_id,
        )
    )
    await PostgresProjectContentLinkWriter(session).inherit_capture_links(
        InheritCaptureProjectLinksCommand(
            access_context=command.access_context,
            source_capture_event_id=capture_event_id,
            content_kind=ProjectContentKind(candidate.candidate_type.value),
            content_id=record.id,
            created_at=command.completed_at,
            trace_id=trace_id,
        )
    )
    return record.id


def _stored_candidate(
    candidate: GroundedCandidate, materialized_record_id: UUID | None
) -> StoredCandidate:
    status = {
        CandidateDisposition.MATERIALIZE: CandidateStorageStatus.MATERIALIZED,
        CandidateDisposition.NEEDS_REVIEW: CandidateStorageStatus.NEEDS_REVIEW,
        CandidateDisposition.ALREADY_CAPTURED: CandidateStorageStatus.ALREADY_CAPTURED,
    }[candidate.disposition]
    return StoredCandidate(
        candidate_type=candidate.candidate_type,
        source_quote=candidate.source_quote,
        modality=candidate.modality,
        confidence=candidate.confidence,
        status=status,
        validation_code=candidate.validation_code,
        materialized_record_id=materialized_record_id,
    )

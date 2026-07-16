from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.bootstrap.task_capture_in_transaction import (
    build_task_capture,
    send_reminder_confirmations,
)
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresVoiceAttachmentWriter,
)
from second_brain.slices.capture.application.contracts import MarkVoiceStoredCommand
from second_brain.slices.identity.application.contracts import WorkerIdentityPort
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkWriter,
)
from second_brain.slices.projects.application.contracts import (
    InheritCaptureProjectLinksCommand,
)
from second_brain.slices.projects.domain.entities import ProjectContentKind
from second_brain.slices.reminders.application.contracts import ReminderDeliveryPort
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.tasks.application.contracts import CreateTypedCaptureCommand
from second_brain.slices.tasks.domain.entities import PendingCaptureType


class EmptyTranscriptError(RuntimeError):
    safe_error_code = "empty_transcript"


class VoiceDownloadCompletionInTransaction:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def complete(self, command: CompleteVoiceDownloadCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresVoiceAttachmentWriter(session).mark_stored(
                    MarkVoiceStoredCommand(
                        access_context=command.access_context,
                        capture_event_id=command.capture_event_id,
                        storage_key=command.stored_voice.storage_key,
                        sha256=command.stored_voice.sha256,
                        stored_size=command.stored_voice.size,
                        stored_mime_type=command.stored_voice.mime_type,
                        stored_at=command.completed_at,
                    )
                )
                await PostgresProcessingWriter(session).complete_voice_download(command)


class VoiceTranscriptionCompletionInTransaction:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        delivery_port: ReminderDeliveryPort,
        identity: WorkerIdentityPort,
    ) -> None:
        self._session_factory = session_factory
        self._delivery_port = delivery_port
        self._identity = identity

    async def complete(self, command: CompleteVoiceTranscriptionCommand) -> None:
        if not command.draft.text.strip():
            raise EmptyTranscriptError
        # (remind_at UTC, tz пространства) созданных здесь напоминаний — для
        # подтверждения «⏰ Напомню…» после коммита.
        confirmations: list[tuple[datetime, str]] = []
        async with self._session_factory() as session:
            async with session.begin():
                processing = PostgresProcessingWriter(session)
                target = await processing.lock_transcription_target(
                    command.access_context, command.step_id
                )
                record = await build_task_capture(
                    session,
                    lambda remind_at, tz: confirmations.append((remind_at, tz)),
                ).create_for_selection(
                    CreateTypedCaptureCommand(
                        access_context=command.access_context,
                        selection=PendingCaptureType(target.output_type.value),
                        text=command.draft.text,
                        source_capture_event_id=target.capture_event_id,
                        created_at=command.completed_at,
                        trace_id=target.trace_id,
                    )
                )
                await PostgresSemanticIndexWriter(session).register_target(
                    RegisterIndexingTargetCommand(
                        access_context=command.access_context,
                        processing_run_id=target.run_id,
                        record_kind=SearchRecordType(target.output_type.value),
                        record_id=record.id,
                        created_at=command.completed_at,
                        trace_id=target.trace_id,
                    )
                )
                await PostgresProjectContentLinkWriter(session).inherit_capture_links(
                    InheritCaptureProjectLinksCommand(
                        access_context=command.access_context,
                        source_capture_event_id=target.capture_event_id,
                        content_kind=ProjectContentKind(target.output_type.value),
                        content_id=record.id,
                        created_at=command.completed_at,
                        trace_id=target.trace_id,
                    )
                )
                await processing.complete_voice_transcription(command)
        # После коммита: голосовая задача со временем иначе молчала бы —
        # владелец не знал бы, что будильник заведён. Осознанный lean-край: сбой
        # между коммитом и отправкой теряет/дублирует ТОЛЬКО подтверждение.
        await send_reminder_confirmations(
            self._delivery_port, self._identity, command.access_context, confirmations
        )

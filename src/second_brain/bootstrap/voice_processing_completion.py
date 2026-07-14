from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresVoiceAttachmentWriter,
)
from second_brain.slices.capture.application.contracts import MarkVoiceStoredCommand
from second_brain.slices.knowledge.adapters.persistence.repository import (
    PostgresKnowledgeWriter,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
)
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresPendingCaptureSelectionWriter,
    PostgresTaskWriter,
)
from second_brain.slices.tasks.application.contracts import CreateTypedCaptureCommand
from second_brain.slices.tasks.application.task_capture import TaskCapture
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
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def complete(self, command: CompleteVoiceTranscriptionCommand) -> None:
        if not command.draft.text.strip():
            raise EmptyTranscriptError
        async with self._session_factory() as session:
            async with session.begin():
                processing = PostgresProcessingWriter(session)
                target = await processing.lock_transcription_target(
                    command.access_context, command.step_id
                )
                await TaskCapture(
                    PostgresPendingCaptureSelectionWriter(session),
                    PostgresTaskWriter(session),
                    PostgresKnowledgeWriter(session),
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
                await processing.complete_voice_transcription(command)

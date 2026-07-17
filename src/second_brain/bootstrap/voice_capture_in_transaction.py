from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventWriter,
)
from second_brain.slices.capture.application.capture_voice import CaptureVoice
from second_brain.slices.capture.application.contracts import (
    CaptureVoiceCommand,
    CaptureVoicePort,
)
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import UpdateTransaction
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CreateVoiceProcessingRunCommand,
)
from second_brain.slices.processing.domain.entities import TranscriptionOutputType
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkWriter,
)
from second_brain.slices.projects.application.contracts import (
    LinkCurrentProjectToCaptureCommand,
)
from second_brain.slices.tasks.adapters.persistence.repository import (
    PostgresPendingCaptureSelectionWriter,
)
from second_brain.slices.tasks.application.contracts import (
    ConsumePendingCaptureSelectionCommand,
)
from second_brain.slices.tasks.application.task_capture import TaskCapture
from second_brain.slices.tasks.domain.entities import PendingCaptureType


class VoiceCaptureInTransaction(CaptureVoicePort):
    """Atomically freezes type, stores voice metadata, and queues processing."""

    async def capture(
        self, command: CaptureVoiceCommand, transaction: UpdateTransaction
    ) -> CaptureEvent:
        if not isinstance(transaction, PostgresUpdateTransaction):
            raise TypeError("voice capture requires the PostgreSQL update transaction")
        session = transaction.active_session
        selection = await TaskCapture(
            PostgresPendingCaptureSelectionWriter(session)
        ).consume_selection(
            ConsumePendingCaptureSelectionCommand(
                access_context=command.access_context,
                consumed_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        # selection is None → кнопку не нажимали: замораживаем NOTE как ДЕФОЛТ,
        # который при расшифровке со временем маршрутизируется в задачу. Явно
        # выбранный тип (в т.ч. «Заметка») уважаем как есть.
        frozen_type = selection or PendingCaptureType.NOTE
        source = await CaptureVoice(PostgresCaptureEventWriter(session)).execute(
            command
        )
        await PostgresProjectContentLinkWriter(session).link_current_to_capture(
            LinkCurrentProjectToCaptureCommand(
                access_context=command.access_context,
                capture_event_id=source.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        await PostgresProcessingWriter(session).create_voice_run(
            CreateVoiceProcessingRunCommand(
                access_context=command.access_context,
                capture_event_id=source.id,
                output_type=TranscriptionOutputType(frozen_type.value),
                route_default_by_time=selection is None,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        return source

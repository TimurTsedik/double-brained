"""Bootstrap-композиция приёма фото в receipt-транзакции.

Фото С ПОДПИСЬЮ = обычный маршрут типизации текста (кнопка/время/дефолт-
заметка): текст записи = caption ДОСЛОВНО, ссылки подписи — sidecar'ом, плюс
прогон IMAGE_DOWNLOAD + CLASSIFICATION + INDEXING. Фото БЕЗ ПОДПИСИ: только
immutable CaptureEvent(image) + attachment + source-only прогон (один шаг
IMAGE_DOWNLOAD) — typed-запись НЕ создаётся, ничего не выдумывается.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.bootstrap.task_capture_in_transaction import (
    build_task_capture,
    record_output_type,
    record_project_kind,
    record_weblink_kind,
)
from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventWriter,
)
from second_brain.slices.capture.application.capture_image import CaptureImage
from second_brain.slices.capture.application.contracts import (
    CaptureImageCommand,
    CaptureImagePort,
    CaptureImageResult,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import UpdateTransaction
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CreateImageProcessingRunCommand,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkWriter,
)
from second_brain.slices.projects.application.contracts import (
    InheritCaptureProjectLinksCommand,
    LinkCurrentProjectToCaptureCommand,
)
from second_brain.slices.retrieval.adapters.persistence.repository import (
    PostgresSemanticIndexWriter,
)
from second_brain.slices.retrieval.application.contracts import (
    RegisterIndexingTargetCommand,
)
from second_brain.slices.retrieval.domain.entities import SearchRecordType
from second_brain.slices.tasks.application.contracts import (
    ConsumePendingTaskTextCommand,
)
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresWeblinkWriter,
)
from second_brain.slices.weblinks.application.contracts import (
    RecordUrlEntry,
    SaveRecordLinksCommand,
)


class ImageCaptureInTransaction(CaptureImagePort):
    """Атомарно: журнал+attachment, запись из подписи (если есть) и прогон."""

    async def capture(
        self, command: CaptureImageCommand, transaction: UpdateTransaction
    ) -> CaptureImageResult:
        session = _active_session(transaction)
        source = await CaptureImage(PostgresCaptureEventWriter(session)).execute(
            command
        )
        project_links = PostgresProjectContentLinkWriter(session)
        await project_links.link_current_to_capture(
            LinkCurrentProjectToCaptureCommand(
                access_context=command.access_context,
                capture_event_id=source.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        # Подпись идёт тем же маршрутом, что текст (кнопка/время/дефолт);
        # пустая подпись — record None, запись не выдумывается.
        record = await build_task_capture(session).consume_for_text(
            ConsumePendingTaskTextCommand(
                access_context=command.access_context,
                text=command.caption or None,
                is_private_chat=True,
                telegram_message_id=command.telegram_message_id,
                source_capture_event_id=source.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        if record is None:
            # Source-only прогон: единственный шаг — скачивание оригинала.
            await PostgresProcessingWriter(session).create_image_run(
                CreateImageProcessingRunCommand(
                    access_context=command.access_context,
                    capture_event_id=source.id,
                    output_type=None,
                    created_at=command.received_at,
                    trace_id=command.trace_id,
                )
            )
            return CaptureImageResult(source=source, record_created=False)
        await project_links.inherit_capture_links(
            InheritCaptureProjectLinksCommand(
                access_context=command.access_context,
                source_capture_event_id=source.id,
                content_kind=record_project_kind(record),
                content_id=record.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        run = await PostgresProcessingWriter(session).create_image_run(
            CreateImageProcessingRunCommand(
                access_context=command.access_context,
                capture_event_id=source.id,
                output_type=record_output_type(record),
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        await PostgresSemanticIndexWriter(session).register_target(
            RegisterIndexingTargetCommand(
                access_context=command.access_context,
                processing_run_id=run.id,
                record_kind=SearchRecordType(record_output_type(record).value),
                record_id=record.id,
                created_at=command.received_at,
                trace_id=command.trace_id,
            )
        )
        # Ссылки подписи — sidecar'ом тем же коммитом (текст записи дословный).
        if command.links:
            await PostgresWeblinkWriter(session).save_links(
                SaveRecordLinksCommand(
                    access_context=command.access_context,
                    record_kind=record_weblink_kind(record),
                    record_id=record.id,
                    entries=tuple(
                        RecordUrlEntry(label=link.label, url=link.url)
                        for link in command.links
                    ),
                    created_at=command.received_at,
                    trace_id=command.trace_id,
                )
            )
        return CaptureImageResult(source=source, record_created=True)


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("image capture requires the PostgreSQL update transaction")
    return transaction.active_session

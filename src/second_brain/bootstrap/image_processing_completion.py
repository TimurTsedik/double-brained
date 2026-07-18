"""Завершение IMAGE_DOWNLOAD одной транзакцией: mark_stored + succeed шага.

Зеркалит VoiceDownloadCompletionInTransaction: метаданные хранения (storage_key,
sha256, размер, mime, момент) пишутся на attachment строго вместе с успехом
шага — половинчатых состояний нет.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresImageAttachmentWriter,
)
from second_brain.slices.capture.application.contracts import MarkImageStoredCommand
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingWriter,
)
from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
)


class ImageDownloadCompletionInTransaction:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def complete(self, command: CompleteImageDownloadCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresImageAttachmentWriter(session).mark_stored(
                    MarkImageStoredCommand(
                        access_context=command.access_context,
                        capture_event_id=command.capture_event_id,
                        storage_key=command.stored_image.storage_key,
                        sha256=command.stored_image.sha256,
                        stored_size=command.stored_image.size,
                        stored_mime_type=command.stored_image.mime_type,
                        stored_at=command.completed_at,
                    )
                )
                await PostgresProcessingWriter(session).complete_image_download(command)

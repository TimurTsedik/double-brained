"""Шаг IMAGE_DOWNLOAD: скачать оригинал фото, посчитать sha256, отметить хранение.

Зеркалит download-часть VoiceWorker: claim шага → file_id из attachment'а →
controlled bytes из Telegram → immutable-хранилище → completion одной
транзакцией (mark_stored + succeed). Провал — существующий fail_step-путь
(ретраи, затем failure-notice). Дисциплина: «file_id без байтов ≠ сохранено».
"""

import re
from datetime import datetime, timedelta

from second_brain.slices.capture.application.contracts import ImageSourcePort
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
    DownloadImageCommand,
    FailProcessingStepCommand,
    StoreImageCommand,
)
from second_brain.slices.processing.domain.entities import ProcessingStepType
from second_brain.slices.processing.ports.image import (
    ImageDownloadCompletion,
    ImageDownloader,
)
from second_brain.slices.processing.ports.repositories import ProcessingQueue
from second_brain.slices.processing.ports.storage import ImageStorage

DEFAULT_STEP_LEASE = timedelta(minutes=15)
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
IMAGE_STEP_TYPES = (ProcessingStepType.IMAGE_DOWNLOAD,)


class ImageWorker:
    def __init__(
        self,
        *,
        queue: ProcessingQueue,
        image_source: ImageSourcePort,
        downloader: ImageDownloader,
        storage: ImageStorage,
        download_completion: ImageDownloadCompletion,
        step_lease: timedelta = DEFAULT_STEP_LEASE,
    ) -> None:
        if step_lease <= timedelta(0):
            raise ValueError("processing step lease must be positive")
        self._queue = queue
        self._image_source = image_source
        self._downloader = downloader
        self._storage = storage
        self._download_completion = download_completion
        self._step_lease = step_lease

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        claim = await self._queue.claim_due_step(
            access_context,
            now,
            self._step_lease,
            IMAGE_STEP_TYPES,
        )
        if claim is None:
            return False
        try:
            source = await self._image_source.get_image_source(
                access_context, claim.capture_event_id
            )
            downloaded = await self._downloader.download(
                DownloadImageCommand(file_id=source.file_id)
            )
            stored = await self._storage.store(
                StoreImageCommand(
                    access_context=access_context,
                    capture_event_id=claim.capture_event_id,
                    content=downloaded.content,
                )
            )
            await self._download_completion.complete(
                CompleteImageDownloadCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    capture_event_id=claim.capture_event_id,
                    stored_image=stored,
                    completed_at=now,
                )
            )
        except Exception as error:
            await self._queue.fail_step(
                FailProcessingStepCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    failed_at=now,
                    safe_error_code=_safe_error_code(error),
                )
            )
        return True


def _safe_error_code(error: Exception) -> str:
    value = getattr(error, "safe_error_code", None)
    if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value):
        return value
    return "processing_failed"

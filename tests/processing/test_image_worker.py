"""ImageWorker: happy-path скачивания оригинала и мягкие отказы safe-кодами."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from second_brain.slices.capture.application.contracts import TelegramImageSource
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.storage.local_image_storage import (
    ImageStorageFailure,
)
from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
    DownloadedImage,
    DownloadImageCommand,
    StoredImage,
    StoreImageCommand,
)
from second_brain.slices.processing.application.image_worker import ImageWorker
from second_brain.slices.processing.domain.entities import (
    ProcessingStepClaim,
    ProcessingStepType,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
CAPTURE_ID = UUID("00000000-0000-0000-0000-000000000101")
RUN_ID = UUID("00000000-0000-0000-0000-000000000201")
STEP_ID = UUID("00000000-0000-0000-0000-000000000301")
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"private image"


def claim() -> ProcessingStepClaim:
    return ProcessingStepClaim(
        step_id=STEP_ID,
        run_id=RUN_ID,
        capture_event_id=CAPTURE_ID,
        step_type=ProcessingStepType.IMAGE_DOWNLOAD,
        # Source-only прогон: фото без подписи, типа нет — download работает.
        output_type=None,
        attempt_count=1,
        lease_expires_at=NOW + timedelta(minutes=15),
        trace_id="1" * 32,
    )


class Queue:
    def __init__(self, claimed: ProcessingStepClaim | None) -> None:
        self.claimed = claimed
        self.claim_calls: list[tuple[AccessContext, datetime, timedelta, tuple]] = []
        self.failures: list[object] = []

    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
        step_types: tuple[ProcessingStepType, ...],
    ) -> ProcessingStepClaim | None:
        self.claim_calls.append((access_context, now, lease_duration, step_types))
        return self.claimed

    async def fail_step(self, command: object) -> object:
        self.failures.append(command)
        return object()

    async def skip_step(self, command: object) -> object:
        raise AssertionError("image download must never skip")


class ImageSource:
    def __init__(self) -> None:
        self.calls: list[tuple[AccessContext, UUID]] = []

    async def get_image_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramImageSource:
        self.calls.append((access_context, capture_event_id))
        return TelegramImageSource(file_id="private-photo-id")


class Downloader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.commands: list[DownloadImageCommand] = []

    async def download(self, command: DownloadImageCommand) -> DownloadedImage:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return DownloadedImage(content=JPEG_BYTES)


class Storage:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.store_commands: list[StoreImageCommand] = []

    async def store(self, command: StoreImageCommand) -> StoredImage:
        self.store_commands.append(command)
        if self.error is not None:
            raise self.error
        return StoredImage(
            storage_key=f"{ACCESS.user_space_id}/{CAPTURE_ID}/original.jpg",
            local_path="/private/local/original.jpg",
            sha256="a" * 64,
            size=len(command.content),
            mime_type="image/jpeg",
        )


class Completion:
    def __init__(self) -> None:
        self.commands: list[CompleteImageDownloadCommand] = []

    async def complete(self, command: CompleteImageDownloadCommand) -> None:
        self.commands.append(command)


def worker(
    queue: Queue,
    downloader: Downloader | None = None,
    storage: Storage | None = None,
    completion: Completion | None = None,
) -> ImageWorker:
    return ImageWorker(
        queue=queue,
        image_source=ImageSource(),
        downloader=downloader or Downloader(),
        storage=storage or Storage(),
        download_completion=completion or Completion(),
    )


@pytest.mark.asyncio
async def test_worker_claims_only_image_download_steps() -> None:
    queue = Queue(None)

    processed = await worker(queue).process_once(ACCESS, NOW)

    assert processed is False
    assert queue.claim_calls == [
        (ACCESS, NOW, timedelta(minutes=15), (ProcessingStepType.IMAGE_DOWNLOAD,))
    ]


@pytest.mark.asyncio
async def test_happy_path_downloads_stores_and_completes_with_checksum() -> None:
    queue = Queue(claim())
    downloader = Downloader()
    storage = Storage()
    completion = Completion()

    processed = await worker(queue, downloader, storage, completion).process_once(
        ACCESS, NOW
    )

    assert processed is True
    assert queue.failures == []
    assert len(storage.store_commands) == 1
    assert storage.store_commands[0].content == JPEG_BYTES
    assert len(completion.commands) == 1
    command = completion.commands[0]
    assert command.step_id == STEP_ID
    assert command.capture_event_id == CAPTURE_ID
    assert command.stored_image.sha256 == "a" * 64
    assert command.completed_at == NOW
    assert "private image" not in repr(command)


@pytest.mark.asyncio
async def test_oversized_image_fails_step_softly_with_safe_code() -> None:
    queue = Queue(claim())
    completion = Completion()

    processed = await worker(
        queue,
        storage=Storage(error=ImageStorageFailure("image_too_large")),
        completion=completion,
    ).process_once(ACCESS, NOW)

    assert processed is True
    assert completion.commands == []
    assert len(queue.failures) == 1
    assert queue.failures[0].safe_error_code == "image_too_large"


@pytest.mark.asyncio
async def test_download_error_fails_step_with_generic_safe_code() -> None:
    queue = Queue(claim())

    await worker(
        queue, downloader=Downloader(error=RuntimeError("secret boom"))
    ).process_once(ACCESS, NOW)

    assert len(queue.failures) == 1
    assert queue.failures[0].safe_error_code == "processing_failed"

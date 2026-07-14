from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from second_brain.slices.capture.application.contracts import TelegramVoiceSource
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    DownloadedVoice,
    LocateVoiceCommand,
    StoredVoice,
    StoredVoiceLocation,
    StoreVoiceCommand,
    TranscribeVoiceCommand,
    TranscriptionDraft,
)
from second_brain.slices.processing.application.voice_worker import VoiceWorker
from second_brain.slices.processing.domain.entities import (
    ProcessingStepClaim,
    ProcessingStepType,
    TranscriptionOutputType,
    TranscriptSegment,
    TranscriptWord,
)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
CAPTURE_ID = UUID("00000000-0000-0000-0000-000000000101")
RUN_ID = UUID("00000000-0000-0000-0000-000000000201")
STEP_ID = UUID("00000000-0000-0000-0000-000000000301")


def claim(step_type: ProcessingStepType, attempt: int = 1) -> ProcessingStepClaim:
    return ProcessingStepClaim(
        step_id=STEP_ID,
        run_id=RUN_ID,
        capture_event_id=CAPTURE_ID,
        step_type=step_type,
        output_type=TranscriptionOutputType.NOTE,
        attempt_count=attempt,
        lease_expires_at=NOW + timedelta(minutes=15),
        trace_id="1" * 32,
    )


class Queue:
    def __init__(self, claimed: ProcessingStepClaim | None) -> None:
        self.claimed = claimed
        self.claim_calls: list[
            tuple[
                AccessContext,
                datetime,
                timedelta,
                tuple[ProcessingStepType, ...],
            ]
        ] = []
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


class VoiceSource:
    def __init__(self) -> None:
        self.calls: list[tuple[AccessContext, UUID]] = []

    async def get_voice_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramVoiceSource:
        self.calls.append((access_context, capture_event_id))
        return TelegramVoiceSource(
            file_id="private-file-id",
            mime_type="audio/ogg",
        )


class Downloader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.commands: list[object] = []

    async def download(self, command: object) -> DownloadedVoice:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return DownloadedVoice(content=b"private voice", mime_type="audio/ogg")


class Storage:
    def __init__(self) -> None:
        self.store_commands: list[StoreVoiceCommand] = []
        self.locate_commands: list[LocateVoiceCommand] = []

    async def store(self, command: StoreVoiceCommand) -> StoredVoice:
        self.store_commands.append(command)
        return StoredVoice(
            storage_key=f"{ACCESS.user_space_id}/{CAPTURE_ID}/original.ogg",
            local_path="/private/local/original.ogg",
            sha256="a" * 64,
            size=len(command.content),
            mime_type=command.mime_type or "audio/ogg",
        )

    async def locate(self, command: LocateVoiceCommand) -> StoredVoiceLocation:
        self.locate_commands.append(command)
        return StoredVoiceLocation(local_path="/private/local/original.ogg")


class DownloadCompletion:
    def __init__(self) -> None:
        self.commands: list[CompleteVoiceDownloadCommand] = []

    async def complete(self, command: CompleteVoiceDownloadCommand) -> None:
        self.commands.append(command)


class Model:
    def __init__(self) -> None:
        self.commands: list[TranscribeVoiceCommand] = []

    async def transcribe(self, command: TranscribeVoiceCommand) -> TranscriptionDraft:
        self.commands.append(command)
        return TranscriptionDraft(
            text="private transcript",
            language="ru",
            language_probability=None,
            model_name="local-model",
            segments=(
                TranscriptSegment(
                    0.0,
                    1.0,
                    "private transcript",
                    (TranscriptWord(0.0, 1.0, "private transcript"),),
                ),
            ),
        )


class TranscriptionCompletion:
    def __init__(self) -> None:
        self.commands: list[CompleteVoiceTranscriptionCommand] = []

    async def complete(self, command: CompleteVoiceTranscriptionCommand) -> None:
        self.commands.append(command)


def worker(
    queue: Queue,
    *,
    downloader: Downloader | None = None,
) -> tuple[
    VoiceWorker,
    VoiceSource,
    Downloader,
    Storage,
    DownloadCompletion,
    Model,
    TranscriptionCompletion,
]:
    source = VoiceSource()
    actual_downloader = downloader or Downloader()
    storage = Storage()
    download_completion = DownloadCompletion()
    model = Model()
    transcription_completion = TranscriptionCompletion()
    return (
        VoiceWorker(
            queue=queue,
            voice_source=source,
            downloader=actual_downloader,
            storage=storage,
            download_completion=download_completion,
            transcription_model=model,
            transcription_completion=transcription_completion,
        ),
        source,
        actual_downloader,
        storage,
        download_completion,
        model,
        transcription_completion,
    )


@pytest.mark.asyncio
async def test_no_claim_means_no_external_work() -> None:
    queue = Queue(None)
    app, source, downloader, storage, download_done, model, transcript_done = worker(
        queue
    )

    worked = await app.process_once(ACCESS, NOW)

    assert worked is False
    assert queue.claim_calls == [
        (
            ACCESS,
            NOW,
            timedelta(minutes=15),
            (
                ProcessingStepType.AUDIO_DOWNLOAD,
                ProcessingStepType.TRANSCRIPTION,
            ),
        )
    ]
    assert source.calls == []
    assert downloader.commands == []
    assert storage.store_commands == []
    assert storage.locate_commands == []
    assert download_done.commands == []
    assert model.commands == []
    assert transcript_done.commands == []


@pytest.mark.asyncio
async def test_download_step_reads_downloads_stores_and_completes_in_scope() -> None:
    queue = Queue(claim(ProcessingStepType.AUDIO_DOWNLOAD))
    app, source, downloader, storage, download_done, model, transcript_done = worker(
        queue
    )

    worked = await app.process_once(ACCESS, NOW)

    assert worked is True
    assert source.calls == [(ACCESS, CAPTURE_ID)]
    assert len(downloader.commands) == 1
    assert "private-file-id" not in repr(downloader.commands[0])
    assert storage.store_commands == [
        StoreVoiceCommand(
            access_context=ACCESS,
            capture_event_id=CAPTURE_ID,
            content=b"private voice",
            mime_type="audio/ogg",
        )
    ]
    assert len(download_done.commands) == 1
    completed = download_done.commands[0]
    assert completed.access_context == ACCESS
    assert completed.step_id == STEP_ID
    assert completed.capture_event_id == CAPTURE_ID
    assert completed.completed_at == NOW
    assert model.commands == []
    assert transcript_done.commands == []
    assert queue.failures == []


@pytest.mark.asyncio
async def test_transcription_step_locates_audio_runs_mlx_and_completes() -> None:
    queue = Queue(claim(ProcessingStepType.TRANSCRIPTION))
    app, source, downloader, storage, download_done, model, transcript_done = worker(
        queue
    )

    worked = await app.process_once(ACCESS, NOW)

    assert worked is True
    assert source.calls == []
    assert downloader.commands == []
    assert storage.locate_commands == [
        LocateVoiceCommand(access_context=ACCESS, capture_event_id=CAPTURE_ID)
    ]
    assert len(model.commands) == 1
    assert "/private/local/original.ogg" not in repr(model.commands[0])
    assert len(transcript_done.commands) == 1
    completed = transcript_done.commands[0]
    assert completed.access_context == ACCESS
    assert completed.step_id == STEP_ID
    assert completed.completed_at == NOW
    assert "private transcript" not in repr(completed)
    assert download_done.commands == []
    assert queue.failures == []


class UnsafeProviderFailure(RuntimeError):
    safe_error_code = "telegram_download_failed"


@pytest.mark.asyncio
async def test_external_failure_records_only_safe_code_for_retry() -> None:
    queue = Queue(claim(ProcessingStepType.AUDIO_DOWNLOAD, attempt=1))
    app, source, downloader, storage, download_done, model, transcript_done = worker(
        queue,
        downloader=Downloader(
            UnsafeProviderFailure("private file id and provider response")
        ),
    )

    worked = await app.process_once(ACCESS, NOW)

    assert worked is True
    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure.access_context == ACCESS
    assert failure.step_id == STEP_ID
    assert failure.failed_at == NOW
    assert failure.safe_error_code == "telegram_download_failed"
    assert "private file id" not in repr(failure)
    assert source.calls == [(ACCESS, CAPTURE_ID)]
    assert len(downloader.commands) == 1
    assert storage.store_commands == []
    assert download_done.commands == []
    assert model.commands == []
    assert transcript_done.commands == []


class OversizedSafeCodeFailure(RuntimeError):
    safe_error_code = "a" * 65


@pytest.mark.asyncio
async def test_error_code_outside_database_contract_uses_fixed_fallback() -> None:
    queue = Queue(claim(ProcessingStepType.AUDIO_DOWNLOAD))
    app, *_ = worker(
        queue,
        downloader=Downloader(OversizedSafeCodeFailure("private payload")),
    )

    await app.process_once(ACCESS, NOW)

    assert len(queue.failures) == 1
    assert queue.failures[0].safe_error_code == "processing_failed"

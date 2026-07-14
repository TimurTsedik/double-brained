import re
from datetime import datetime, timedelta

from second_brain.slices.capture.application.contracts import VoiceSourcePort
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    DownloadVoiceCommand,
    FailProcessingStepCommand,
    LocateVoiceCommand,
    StoreVoiceCommand,
    TranscribeVoiceCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepClaim,
    ProcessingStepType,
)
from second_brain.slices.processing.ports.repositories import ProcessingQueue
from second_brain.slices.processing.ports.storage import VoiceStorage
from second_brain.slices.processing.ports.transcription import TranscriptionModel
from second_brain.slices.processing.ports.voice import (
    VoiceDownloadCompletion,
    VoiceDownloader,
    VoiceTranscriptionCompletion,
)

DEFAULT_STEP_LEASE = timedelta(minutes=15)
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
VOICE_STEP_TYPES = (
    ProcessingStepType.AUDIO_DOWNLOAD,
    ProcessingStepType.TRANSCRIPTION,
)


class VoiceWorker:
    def __init__(
        self,
        *,
        queue: ProcessingQueue,
        voice_source: VoiceSourcePort,
        downloader: VoiceDownloader,
        storage: VoiceStorage,
        download_completion: VoiceDownloadCompletion,
        transcription_model: TranscriptionModel,
        transcription_completion: VoiceTranscriptionCompletion,
        step_lease: timedelta = DEFAULT_STEP_LEASE,
    ) -> None:
        if step_lease <= timedelta(0):
            raise ValueError("processing step lease must be positive")
        self._queue = queue
        self._voice_source = voice_source
        self._downloader = downloader
        self._storage = storage
        self._download_completion = download_completion
        self._transcription_model = transcription_model
        self._transcription_completion = transcription_completion
        self._step_lease = step_lease

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        claim = await self._queue.claim_due_step(
            access_context,
            now,
            self._step_lease,
            VOICE_STEP_TYPES,
        )
        if claim is None:
            return False
        try:
            if claim.step_type is ProcessingStepType.AUDIO_DOWNLOAD:
                await self._download(access_context, claim, now)
            else:
                await self._transcribe(access_context, claim, now)
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

    async def _download(
        self,
        access_context: AccessContext,
        claim: ProcessingStepClaim,
        now: datetime,
    ) -> None:
        source = await self._voice_source.get_voice_source(
            access_context, claim.capture_event_id
        )
        downloaded = await self._downloader.download(
            DownloadVoiceCommand(
                file_id=source.file_id,
                mime_type=source.mime_type,
            )
        )
        stored = await self._storage.store(
            StoreVoiceCommand(
                access_context=access_context,
                capture_event_id=claim.capture_event_id,
                content=downloaded.content,
                mime_type=downloaded.mime_type,
            )
        )
        await self._download_completion.complete(
            CompleteVoiceDownloadCommand(
                access_context=access_context,
                step_id=claim.step_id,
                capture_event_id=claim.capture_event_id,
                stored_voice=stored,
                completed_at=now,
            )
        )

    async def _transcribe(
        self,
        access_context: AccessContext,
        claim: ProcessingStepClaim,
        now: datetime,
    ) -> None:
        location = await self._storage.locate(
            LocateVoiceCommand(
                access_context=access_context,
                capture_event_id=claim.capture_event_id,
            )
        )
        draft = await self._transcription_model.transcribe(
            TranscribeVoiceCommand(local_path=location.local_path)
        )
        await self._transcription_completion.complete(
            CompleteVoiceTranscriptionCommand(
                access_context=access_context,
                step_id=claim.step_id,
                draft=draft,
                completed_at=now,
            )
        )


def _safe_error_code(error: Exception) -> str:
    value = getattr(error, "safe_error_code", None)
    if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value):
        return value
    return "processing_failed"

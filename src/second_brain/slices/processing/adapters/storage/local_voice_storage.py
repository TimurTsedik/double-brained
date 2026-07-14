import asyncio
import os
import tempfile
from hashlib import sha256
from pathlib import Path, PurePosixPath

from second_brain.slices.processing.application.contracts import (
    LocateVoiceCommand,
    StoredVoice,
    StoredVoiceLocation,
    StoreVoiceCommand,
)

DEFAULT_MAX_VOICE_BYTES = 25 * 1024 * 1024
DEFAULT_VOICE_MIME_TYPE = "audio/ogg"


class VoiceStorageFailure(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class LocalVoiceStorage:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_VOICE_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("voice storage limit must be positive")
        self._root = Path(root).expanduser().resolve()
        self._max_bytes = max_bytes

    async def prepare(self) -> None:
        await asyncio.to_thread(self._prepare_sync)

    async def store(self, command: StoreVoiceCommand) -> StoredVoice:
        if len(command.content) > self._max_bytes:
            raise VoiceStorageFailure("audio_too_large")
        return await asyncio.to_thread(self._store_sync, command)

    async def locate(self, command: LocateVoiceCommand) -> StoredVoiceLocation:
        return await asyncio.to_thread(self._locate_sync, command)

    def _prepare_sync(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._root, prefix=".voice-preflight-", suffix=".tmp"
            )
            os.close(descriptor)
            Path(temporary_name).unlink()
        except Exception:
            raise VoiceStorageFailure("storage_unavailable") from None

    def _store_sync(self, command: StoreVoiceCommand) -> StoredVoice:
        relative = PurePosixPath(
            str(command.access_context.user_space_id),
            str(command.capture_event_id),
            "original.ogg",
        )
        destination = self._root.joinpath(*relative.parts)
        temporary: Path | None = None
        descriptor = -1
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=".original-",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(command.content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise VoiceStorageFailure("storage_write_failed") from None

        content_hash = sha256(command.content).hexdigest()
        return StoredVoice(
            storage_key=str(relative),
            local_path=str(destination.resolve()),
            sha256=content_hash,
            size=len(command.content),
            mime_type=command.mime_type or DEFAULT_VOICE_MIME_TYPE,
        )

    def _locate_sync(self, command: LocateVoiceCommand) -> StoredVoiceLocation:
        destination = (
            self._root
            / str(command.access_context.user_space_id)
            / str(command.capture_event_id)
            / "original.ogg"
        )
        if not destination.is_file():
            raise VoiceStorageFailure("audio_missing")
        return StoredVoiceLocation(local_path=str(destination.resolve()))

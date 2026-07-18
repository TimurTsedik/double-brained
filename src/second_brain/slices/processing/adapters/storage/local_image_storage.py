"""Локальное immutable-хранилище оригиналов фото.

Зеркалит LocalVoiceStorage (atomic write через tmp+replace, sha256), но mime
Telegram у фото не отдаёт — тип определяется sniffing'ом магических байтов по
whitelist'у изображений; расширение файла выводится из mime. Ключ —
``{user_space_id}/{capture_event_id}/original.<ext>``: изоляция пространств
на уровне путей, как у голоса.
"""

import asyncio
import os
import tempfile
from hashlib import sha256
from pathlib import Path, PurePosixPath

from second_brain.slices.processing.application.contracts import (
    StoredImage,
    StoreImageCommand,
)

DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Whitelist изображений: (магические байты, mime, расширение). Telegram-фото —
# всегда JPEG, PNG/WebP оставлены на пересылку файлов будущих дверей.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"RIFF", "image/webp", "webp"),
)


class ImageStorageFailure(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


def sniff_image_mime(content: bytes) -> tuple[str, str] | None:
    """(mime, расширение) по магическим байтам или None для не-изображения."""
    for signature, mime, extension in _SIGNATURES:
        if not content.startswith(signature):
            continue
        if mime == "image/webp" and content[8:12] != b"WEBP":
            continue
        return mime, extension
    return None


class LocalImageStorage:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("image storage limit must be positive")
        self._root = Path(root).expanduser().resolve()
        self._max_bytes = max_bytes

    async def prepare(self) -> None:
        await asyncio.to_thread(self._prepare_sync)

    async def store(self, command: StoreImageCommand) -> StoredImage:
        # Лимит и тип проверяются ДО записи на диск: мягкий отказ safe-кодом,
        # никакого мусора в хранилище.
        if len(command.content) > self._max_bytes:
            raise ImageStorageFailure("image_too_large")
        sniffed = sniff_image_mime(command.content)
        if sniffed is None:
            raise ImageStorageFailure("unsupported_image_type")
        mime, extension = sniffed
        return await asyncio.to_thread(self._store_sync, command, mime, extension)

    def _prepare_sync(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._root, prefix=".image-preflight-", suffix=".tmp"
            )
            os.close(descriptor)
            Path(temporary_name).unlink()
        except Exception:
            raise ImageStorageFailure("storage_unavailable") from None

    def _store_sync(
        self, command: StoreImageCommand, mime: str, extension: str
    ) -> StoredImage:
        relative = PurePosixPath(
            str(command.access_context.user_space_id),
            str(command.capture_event_id),
            f"original.{extension}",
        )
        destination = self._root.joinpath(*relative.parts)
        # Write-once: оригинал неизменяем. Второй скачавший (истёкший lease)
        # НЕ перезаписывает файл — сходимся на уже сохранённых байтах, какое бы
        # расширение ни выдал его sniff (ищем любой original.*).
        existing = self._existing_original(destination.parent)
        if existing is not None:
            return existing
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
            raise ImageStorageFailure("storage_write_failed") from None

        content_hash = sha256(command.content).hexdigest()
        return StoredImage(
            storage_key=str(relative),
            local_path=str(destination.resolve()),
            sha256=content_hash,
            size=len(command.content),
            mime_type=mime,
        )

    def _existing_original(self, directory: Path) -> StoredImage | None:
        """Уже сохранённый original.* этой капчи (метаданные с диска) или None."""
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob("original.*")):
            if candidate.suffix == ".tmp":
                continue
            content = candidate.read_bytes()
            sniffed = sniff_image_mime(content)
            existing_mime = sniffed[0] if sniffed else "application/octet-stream"
            relative = candidate.relative_to(self._root)
            return StoredImage(
                storage_key=str(PurePosixPath(*relative.parts)),
                local_path=str(candidate.resolve()),
                sha256=sha256(content).hexdigest(),
                size=len(content),
                mime_type=existing_mime,
            )
        return None

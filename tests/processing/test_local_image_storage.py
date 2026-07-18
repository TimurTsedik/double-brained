"""Локальное immutable-хранилище оригиналов фото: sniffing, лимит, изоляция."""

from pathlib import Path
from uuid import UUID

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.storage.local_image_storage import (
    ImageStorageFailure,
    LocalImageStorage,
    sniff_image_mime,
)
from second_brain.slices.processing.application.contracts import StoreImageCommand

ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
CAPTURE_ID = UUID("00000000-0000-0000-0000-000000000101")
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-body"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png-body"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"webp-body"


def test_sniffing_recognizes_the_image_whitelist_and_rejects_the_rest() -> None:
    assert sniff_image_mime(JPEG_BYTES) == ("image/jpeg", "jpg")
    assert sniff_image_mime(PNG_BYTES) == ("image/png", "png")
    assert sniff_image_mime(WEBP_BYTES) == ("image/webp", "webp")
    # RIFF-контейнер БЕЗ метки WEBP (например WAV) — не изображение.
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WAVE") is None
    assert sniff_image_mime(b"%PDF-1.7 not an image") is None
    assert sniff_image_mime(b"") is None


@pytest.mark.asyncio
async def test_store_writes_original_with_checksum_under_space_scoped_key(
    tmp_path: Path,
) -> None:
    storage = LocalImageStorage(tmp_path)
    await storage.prepare()

    stored = await storage.store(
        StoreImageCommand(
            access_context=ACCESS,
            capture_event_id=CAPTURE_ID,
            content=JPEG_BYTES,
        )
    )

    # Ключ = {space}/{capture}/original.<ext>: изоляция пространств путями.
    assert stored.storage_key == f"{ACCESS.user_space_id}/{CAPTURE_ID}/original.jpg"
    assert stored.mime_type == "image/jpeg"
    assert stored.size == len(JPEG_BYTES)
    import hashlib

    assert stored.sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert Path(stored.local_path).read_bytes() == JPEG_BYTES


@pytest.mark.asyncio
async def test_second_store_does_not_overwrite_the_immutable_original(
    tmp_path: Path,
) -> None:
    # Write-once: истёкший lease → второй скачавший (даже с ДРУГИМИ байтами,
    # даже другого типа) не перезаписывает оригинал — сходимся на первом файле.
    storage = LocalImageStorage(tmp_path)
    await storage.prepare()
    first = await storage.store(
        StoreImageCommand(
            access_context=ACCESS, capture_event_id=CAPTURE_ID, content=JPEG_BYTES
        )
    )

    second = await storage.store(
        StoreImageCommand(
            access_context=ACCESS, capture_event_id=CAPTURE_ID, content=PNG_BYTES
        )
    )

    assert Path(first.local_path).read_bytes() == JPEG_BYTES
    assert second.storage_key == first.storage_key
    assert second.sha256 == first.sha256
    assert second.mime_type == "image/jpeg"
    assert second.size == first.size


@pytest.mark.asyncio
async def test_oversized_image_is_softly_rejected_before_touching_disk(
    tmp_path: Path,
) -> None:
    storage = LocalImageStorage(tmp_path, max_bytes=8)
    await storage.prepare()

    with pytest.raises(ImageStorageFailure) as failure:
        await storage.store(
            StoreImageCommand(
                access_context=ACCESS,
                capture_event_id=CAPTURE_ID,
                content=JPEG_BYTES,
            )
        )

    assert failure.value.safe_error_code == "image_too_large"
    assert not (tmp_path / str(ACCESS.user_space_id)).exists()


@pytest.mark.asyncio
async def test_non_image_bytes_are_softly_rejected(tmp_path: Path) -> None:
    storage = LocalImageStorage(tmp_path)
    await storage.prepare()

    with pytest.raises(ImageStorageFailure) as failure:
        await storage.store(
            StoreImageCommand(
                access_context=ACCESS,
                capture_event_id=CAPTURE_ID,
                content=b"GIF89a pretender",
            )
        )

    assert failure.value.safe_error_code == "unsupported_image_type"

from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.storage.local_voice_storage import (
    DEFAULT_MAX_VOICE_BYTES,
    LocalVoiceStorage,
    VoiceStorageFailure,
)
from second_brain.slices.processing.application.contracts import (
    LocateVoiceCommand,
    StoreVoiceCommand,
)

ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)
CAPTURE_ID = UUID("00000000-0000-0000-0000-000000000101")


def command(
    content: bytes,
    *,
    access: AccessContext = ACCESS_A,
    mime_type: str | None = "audio/ogg",
) -> StoreVoiceCommand:
    return StoreVoiceCommand(
        access_context=access,
        capture_event_id=CAPTURE_ID,
        content=content,
        mime_type=mime_type,
    )


@pytest.mark.asyncio
async def test_voice_storage_uses_only_trusted_uuid_namespaces_and_checksum(
    tmp_path: Path,
) -> None:
    root = tmp_path / "voice"
    storage = LocalVoiceStorage(root)
    content = b"private ogg bytes"

    stored = await storage.store(command(content))

    expected = root / str(ACCESS_A.user_space_id) / str(CAPTURE_ID) / "original.ogg"
    assert Path(stored.local_path) == expected.resolve()
    assert stored.storage_key == (f"{ACCESS_A.user_space_id}/{CAPTURE_ID}/original.ogg")
    assert expected.read_bytes() == content
    assert stored.sha256 == sha256(content).hexdigest()
    assert stored.size == len(content)
    assert stored.mime_type == "audio/ogg"
    assert list(expected.parent.glob("*.tmp")) == []
    assert str(tmp_path) not in repr(stored)


@pytest.mark.asyncio
async def test_voice_storage_is_idempotent_and_separates_user_spaces(
    tmp_path: Path,
) -> None:
    storage = LocalVoiceStorage(tmp_path / "voice")

    first = await storage.store(command(b"same content"))
    repeated = await storage.store(command(b"same content"))
    other_user = await storage.store(command(b"B content", access=ACCESS_B))

    assert first == repeated
    assert first.storage_key != other_user.storage_key
    assert Path(first.local_path).read_bytes() == b"same content"
    assert Path(other_user.local_path).read_bytes() == b"B content"


@pytest.mark.asyncio
async def test_voice_storage_rejects_content_over_limit_before_writing(
    tmp_path: Path,
) -> None:
    storage = LocalVoiceStorage(tmp_path / "voice", max_bytes=4)

    with pytest.raises(VoiceStorageFailure) as failure:
        await storage.store(command(b"12345"))

    assert failure.value.safe_error_code == "audio_too_large"
    assert list(tmp_path.rglob("*")) == []
    assert DEFAULT_MAX_VOICE_BYTES == 25 * 1024 * 1024


@pytest.mark.asyncio
async def test_failed_atomic_replace_leaves_no_target_or_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "voice"
    storage = LocalVoiceStorage(root)

    def fail_replace(_source: Path, _target: Path) -> Path:
        raise OSError("private provider path must not escape")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(VoiceStorageFailure) as failure:
        await storage.store(command(b"private bytes"))

    target = root / str(ACCESS_A.user_space_id) / str(CAPTURE_ID) / "original.ogg"
    assert failure.value.safe_error_code == "storage_write_failed"
    assert "private provider path" not in str(failure.value)
    assert not target.exists()
    assert list(target.parent.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_missing_mime_type_uses_voice_default(tmp_path: Path) -> None:
    stored = await LocalVoiceStorage(tmp_path / "voice").store(
        command(b"voice", mime_type=None)
    )

    assert stored.mime_type == "audio/ogg"
    assert b"voice" not in repr(stored).encode()


@pytest.mark.asyncio
async def test_storage_preflight_detects_an_unwritable_root(tmp_path: Path) -> None:
    invalid_root = tmp_path / "file-instead-of-directory"
    invalid_root.write_text("occupied", encoding="utf-8")

    with pytest.raises(VoiceStorageFailure) as failure:
        await LocalVoiceStorage(invalid_root).prepare()

    assert failure.value.safe_error_code == "storage_unavailable"
    assert str(invalid_root) not in str(failure.value)


@pytest.mark.asyncio
async def test_locate_returns_only_the_scoped_stored_audio(tmp_path: Path) -> None:
    storage = LocalVoiceStorage(tmp_path / "voice")
    stored = await storage.store(command(b"A voice"))

    located = await storage.locate(
        LocateVoiceCommand(access_context=ACCESS_A, capture_event_id=CAPTURE_ID)
    )

    assert located.local_path == stored.local_path
    assert stored.local_path not in repr(located)
    with pytest.raises(VoiceStorageFailure) as failure:
        await storage.locate(
            LocateVoiceCommand(access_context=ACCESS_B, capture_event_id=CAPTURE_ID)
        )
    assert failure.value.safe_error_code == "audio_missing"

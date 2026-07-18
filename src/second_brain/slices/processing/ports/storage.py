from typing import Protocol

from second_brain.slices.processing.application.contracts import (
    LocateVoiceCommand,
    StoredImage,
    StoredVoice,
    StoredVoiceLocation,
    StoreImageCommand,
    StoreVoiceCommand,
)


class VoiceStorage(Protocol):
    async def store(self, command: StoreVoiceCommand) -> StoredVoice: ...

    async def locate(self, command: LocateVoiceCommand) -> StoredVoiceLocation: ...


class ImageStorage(Protocol):
    """Immutable-хранилище оригиналов фото (locate появится с потребителем)."""

    async def store(self, command: StoreImageCommand) -> StoredImage: ...

from typing import Protocol

from second_brain.slices.processing.application.contracts import (
    LocateVoiceCommand,
    StoredVoice,
    StoredVoiceLocation,
    StoreVoiceCommand,
)


class VoiceStorage(Protocol):
    async def store(self, command: StoreVoiceCommand) -> StoredVoice: ...

    async def locate(self, command: LocateVoiceCommand) -> StoredVoiceLocation: ...

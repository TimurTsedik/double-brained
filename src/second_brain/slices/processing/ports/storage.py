from typing import Protocol

from second_brain.slices.processing.application.contracts import (
    StoredVoice,
    StoreVoiceCommand,
)


class VoiceStorage(Protocol):
    async def store(self, command: StoreVoiceCommand) -> StoredVoice: ...

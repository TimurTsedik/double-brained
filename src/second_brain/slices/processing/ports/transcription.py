from typing import Protocol

from second_brain.slices.processing.application.contracts import (
    TranscribeVoiceCommand,
    TranscriptionDraft,
)


class TranscriptionModel(Protocol):
    async def transcribe(
        self, command: TranscribeVoiceCommand
    ) -> TranscriptionDraft: ...

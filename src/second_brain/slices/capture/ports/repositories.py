from typing import Protocol

from second_brain.slices.capture.application.contracts import (
    CaptureImageCommand,
    CaptureTextCommand,
    CaptureVoiceCommand,
)
from second_brain.slices.capture.domain.entities import CaptureEvent


class CaptureEventWriter(Protocol):
    async def create(self, command: CaptureTextCommand) -> CaptureEvent: ...

    async def create_voice(self, command: CaptureVoiceCommand) -> CaptureEvent: ...

    async def create_image(self, command: CaptureImageCommand) -> CaptureEvent: ...

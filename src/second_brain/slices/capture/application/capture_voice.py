from second_brain.slices.capture.application.contracts import CaptureVoiceCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.capture.ports.repositories import CaptureEventWriter


class CaptureVoice:
    def __init__(self, writer: CaptureEventWriter) -> None:
        self._writer = writer

    async def execute(self, command: CaptureVoiceCommand) -> CaptureEvent:
        return await self._writer.create_voice(command)

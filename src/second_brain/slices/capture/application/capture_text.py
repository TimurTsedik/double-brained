from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.capture.ports.repositories import CaptureEventWriter


class CaptureText:
    def __init__(self, writer: CaptureEventWriter) -> None:
        self._writer = writer

    async def execute(self, command: CaptureTextCommand) -> CaptureEvent:
        return await self._writer.create(command)

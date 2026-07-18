from second_brain.slices.capture.application.contracts import CaptureImageCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.capture.ports.repositories import CaptureEventWriter


class CaptureImage:
    """Immutable-журнал фото: CaptureEvent(image) + attachment одним flush'ем."""

    def __init__(self, writer: CaptureEventWriter) -> None:
        self._writer = writer

    async def execute(self, command: CaptureImageCommand) -> CaptureEvent:
        return await self._writer.create_image(command)

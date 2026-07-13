from datetime import UTC, datetime
from uuid import UUID

import pytest

from second_brain.slices.capture.application.capture_text import CaptureText
from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.capture.ports.repositories import CaptureEventWriter
from second_brain.slices.identity.application.contracts import AccessContext

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


class RecordingCaptureWriter(CaptureEventWriter):
    def __init__(self) -> None:
        self.commands: list[CaptureTextCommand] = []

    async def create(self, command: CaptureTextCommand) -> CaptureEvent:
        self.commands.append(command)
        return CaptureEvent(
            id=UUID("00000000-0000-0000-0000-000000000101"),
            user_space_id=command.access_context.user_space_id,
            channel="telegram",
            bot_id=command.bot_id,
            telegram_update_id=command.telegram_update_id,
            telegram_message_id=command.telegram_message_id,
            raw_text=command.raw_text,
            received_at=command.received_at,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )


@pytest.mark.asyncio
async def test_capture_uses_trusted_scope_and_preserves_raw_text() -> None:
    writer = RecordingCaptureWriter()
    command = CaptureTextCommand(
        access_context=ACCESS_A,
        bot_id=100,
        telegram_update_id=200,
        telegram_message_id=300,
        raw_text="remember this",
        received_at=NOW,
        trace_id="1" * 32,
    )

    event = await CaptureText(writer).execute(command)

    assert event.user_space_id == ACCESS_A.user_space_id
    assert event.raw_text == "remember this"
    assert event.created_at == NOW
    assert writer.commands == [command]

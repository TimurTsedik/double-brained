from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)


@dataclass(frozen=True)
class CaptureTextCommand:
    access_context: AccessContext
    bot_id: int
    telegram_update_id: int
    telegram_message_id: int
    raw_text: str = field(repr=False)
    received_at: datetime
    trace_id: str


class CaptureTextPort(Protocol):
    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent: ...

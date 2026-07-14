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


@dataclass(frozen=True)
class TelegramVoiceMetadata:
    file_id: str = field(repr=False)
    file_unique_id: str = field(repr=False)
    duration_seconds: int
    file_size: int | None
    mime_type: str | None


@dataclass(frozen=True)
class CaptureVoiceCommand:
    access_context: AccessContext
    bot_id: int
    telegram_update_id: int
    telegram_message_id: int
    voice: TelegramVoiceMetadata = field(repr=False)
    received_at: datetime
    trace_id: str


class CaptureTextPort(Protocol):
    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent: ...


class CaptureVoicePort(Protocol):
    async def capture(
        self, command: CaptureVoiceCommand, transaction: UpdateTransaction
    ) -> CaptureEvent: ...

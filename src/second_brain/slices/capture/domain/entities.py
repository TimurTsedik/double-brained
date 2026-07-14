from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID


class CaptureSourceKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"


@dataclass(frozen=True)
class CaptureEvent:
    id: UUID
    user_space_id: UUID
    channel: Literal["telegram"]
    bot_id: int
    telegram_update_id: int
    telegram_message_id: int
    raw_text: str | None = field(repr=False)
    received_at: datetime
    created_at: datetime
    trace_id: str
    source_kind: CaptureSourceKind = CaptureSourceKind.TEXT


@dataclass(frozen=True)
class TelegramAttachment:
    id: UUID
    user_space_id: UUID
    capture_event_id: UUID
    kind: CaptureSourceKind
    telegram_file_id: str = field(repr=False)
    telegram_file_unique_id: str = field(repr=False)
    duration_seconds: int
    telegram_file_size: int | None
    telegram_mime_type: str | None
    storage_key: str | None = field(repr=False)
    sha256: str | None
    stored_size: int | None
    stored_mime_type: str | None
    stored_at: datetime | None
    created_at: datetime
    trace_id: str

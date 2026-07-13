from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class CaptureEvent:
    id: UUID
    user_space_id: UUID
    channel: Literal["telegram"]
    bot_id: int
    telegram_update_id: int
    telegram_message_id: int
    raw_text: str
    received_at: datetime
    created_at: datetime
    trace_id: str

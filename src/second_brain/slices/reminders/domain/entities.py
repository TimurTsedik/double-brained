from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    # Отправка исчерпала бюджет попыток (см. MAX_SEND_ATTEMPTS) — больше не шлём.
    FAILED = "failed"


@dataclass(frozen=True)
class Reminder:
    id: UUID
    user_space_id: UUID
    remind_at: datetime
    # Текст напоминания = заголовок задачи (пользовательский контент) —
    # держим вне repr/логов, как и другие контентные поля слайсов.
    text: str = field(repr=False)
    status: ReminderStatus
    source_task_id: UUID
    created_at: datetime
    updated_at: datetime
    trace_id: str

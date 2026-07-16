from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Contact:
    """Контакт пространства: имя из телеграм-карточки и его номер телефона.

    Имя и номер — PII: держим вне repr/логов, как контентные поля других слайсов.
    """

    id: UUID
    user_space_id: UUID
    display_name: str = field(repr=False)
    phone_number: str = field(repr=False)
    created_at: datetime
    updated_at: datetime
    trace_id: str

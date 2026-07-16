from dataclasses import dataclass, field

from second_brain.slices.capture.application.contracts import TelegramVoiceMetadata
from second_brain.slices.contacts.application.contracts import TelegramContactPayload


@dataclass(frozen=True)
class TelegramUpdate:
    """Normalized Telegram input with text kept transient until trusted routing."""

    bot_id: int
    update_id: int
    is_private: bool
    telegram_user_id: int | None
    text: str | None = field(repr=False)
    telegram_message_id: int | None = None
    callback_query_id: str | None = field(default=None, repr=False)
    callback_data: str | None = field(default=None, repr=False)
    voice: TelegramVoiceMetadata | None = field(default=None, repr=False)
    # Карточка контакта (message.contact): телефон/имя — PII, repr-hidden.
    contact: TelegramContactPayload | None = field(default=None, repr=False)

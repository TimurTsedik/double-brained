from dataclasses import dataclass, field

from second_brain.slices.capture.application.contracts import (
    TelegramLink,
    TelegramPhotoMetadata,
    TelegramVoiceMetadata,
)
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
    # Крупнейший PhotoSize присланного фото (file_id — PII, repr-hidden).
    # У фото-сообщений message.text ПУСТ: подпись едет отдельным полем caption
    # и НЕ участвует в командном парсинге (/start и т.п.).
    photo: TelegramPhotoMetadata | None = field(default=None, repr=False)
    caption: str | None = field(default=None, repr=False)
    # Карточка контакта (message.contact): телефон/имя — PII, repr-hidden.
    contact: TelegramContactPayload | None = field(default=None, repr=False)
    # Ссылки из message.entities / caption_entities (text_link/url) — PII.
    links: tuple[TelegramLink, ...] = field(default=(), repr=False)

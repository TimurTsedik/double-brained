from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)


@dataclass(frozen=True)
class TelegramLink:
    """Ссылка из Telegram-entities: пара «слово → адрес» в порядке появления.

    Для text_link label — подстрока текста, для голого url label = сам url.
    Оба поля — пользовательское содержимое (PII), вне repr/логов.
    """

    label: str = field(repr=False)
    url: str = field(repr=False)


@dataclass(frozen=True)
class CaptureTextCommand:
    access_context: AccessContext
    bot_id: int
    telegram_update_id: int
    telegram_message_id: int
    raw_text: str = field(repr=False)
    received_at: datetime
    trace_id: str
    # Sidecar-ссылки сообщения: текст выше остаётся дословным, пары «слово →
    # адрес» пишутся рядом (record_urls) после создания записи.
    links: tuple[TelegramLink, ...] = field(default=(), repr=False)


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


@dataclass(frozen=True)
class TelegramPhotoMetadata:
    """Метаданные КРУПНЕЙШЕГО PhotoSize присланного фото (file_id — PII)."""

    file_id: str = field(repr=False)
    file_unique_id: str = field(repr=False)
    width: int
    height: int
    file_size: int | None


@dataclass(frozen=True)
class CaptureImageCommand:
    access_context: AccessContext
    bot_id: int
    telegram_update_id: int
    telegram_message_id: int
    photo: TelegramPhotoMetadata = field(repr=False)
    # Подпись фото ДОСЛОВНО (None/пустая → typed-запись не создаётся вовсе).
    caption: str | None = field(repr=False)
    received_at: datetime
    trace_id: str
    # Ссылки из caption_entities — sidecar'ом рядом с записью (как у текста).
    links: tuple[TelegramLink, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class CaptureImageResult:
    """Итог приёма фото: журнал всегда, typed-запись — только при подписи."""

    source: CaptureEvent
    record_created: bool


@dataclass(frozen=True)
class TelegramVoiceSource:
    file_id: str = field(repr=False)
    mime_type: str | None


@dataclass(frozen=True)
class TelegramImageSource:
    file_id: str = field(repr=False)


@dataclass(frozen=True)
class MarkVoiceStoredCommand:
    access_context: AccessContext
    capture_event_id: UUID
    storage_key: str = field(repr=False)
    sha256: str
    stored_size: int
    stored_mime_type: str
    stored_at: datetime


@dataclass(frozen=True)
class MarkImageStoredCommand:
    access_context: AccessContext
    capture_event_id: UUID
    storage_key: str = field(repr=False)
    sha256: str
    stored_size: int
    stored_mime_type: str
    stored_at: datetime


class CaptureTextPort(Protocol):
    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent: ...


class CaptureVoicePort(Protocol):
    async def capture(
        self, command: CaptureVoiceCommand, transaction: UpdateTransaction
    ) -> CaptureEvent: ...


class CaptureImagePort(Protocol):
    async def capture(
        self, command: CaptureImageCommand, transaction: UpdateTransaction
    ) -> CaptureImageResult: ...


class VoiceSourcePort(Protocol):
    async def get_voice_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramVoiceSource: ...


class ImageSourcePort(Protocol):
    async def get_image_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramImageSource: ...

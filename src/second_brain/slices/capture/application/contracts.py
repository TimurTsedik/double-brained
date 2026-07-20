from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.tasks.application.contracts import PendingCaptureType


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
    """Приём текста — из телеграма или из запроса к API.

    Поля с умолчаниями стоят в конце не по вкусу, а по правилу dataclass'а:
    защищённое телеграмное происхождение (``channel="telegram"``) остаётся
    поведением по умолчанию, поэтому вызывающему, который его не знает, форму
    строки в базе испортить нечем — предикат ``ck_capture_events_channel``
    откажет во вставке.
    """

    access_context: AccessContext
    raw_text: str = field(repr=False)
    received_at: datetime
    trace_id: str
    # Происхождение захвата, названное ОДИН раз и явно: маршрут внутри
    # ``TaskCaptureInTransaction.capture`` ветвится по нему, а не гадает по
    # пустому полю.
    channel: str = "telegram"
    bot_id: int | None = None
    telegram_update_id: int | None = None
    telegram_message_id: int | None = None
    # Ключ идемпотентности вызывающего: слепой повтор с тем же значением обязан
    # вернуть ОТВЕТ ПЕРВОГО вызова, а не создать второй захват.
    client_ref: str | None = None
    # Часовой пояс запроса — им разбирается относительное время («завтра в 9»).
    request_tz: str | None = None
    # Происхождение текста: набран или надиктован. Пока чистая пометка.
    modality: str = "text"
    # Явный тип записи из запроса; None = «тип не назван», тогда решает время.
    capture_type: PendingCaptureType | None = None
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

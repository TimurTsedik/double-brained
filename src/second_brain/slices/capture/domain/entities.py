from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CaptureSourceKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    IMAGE = "image"


@dataclass(frozen=True)
class CaptureEvent:
    """Строка журнала захвата — то, что пришло, каким каналом и когда.

    ``channel`` — обычная строка, а не Literal: словарь допустимых значений
    держит предикат ``ck_capture_events_channel`` в базе, и он же — единственная
    авторитетная его версия. Сузить тип здесь значило бы завести вторую версию
    словаря, которая молча разъедется с первой.

    Три телеграмных идентификатора необязательны: захват с телефона их не имеет
    и иметь не может. Форму строки по каждому каналу стережёт тот же предикат —
    NULL'ы здесь не значат «поле можно не заполнить», они значат «этот канал
    таких идентификаторов не рождает».
    """

    id: UUID
    user_space_id: UUID
    channel: str
    bot_id: int | None
    telegram_update_id: int | None
    telegram_message_id: int | None
    raw_text: str | None = field(repr=False)
    received_at: datetime
    created_at: datetime
    trace_id: str
    source_kind: CaptureSourceKind = CaptureSourceKind.TEXT
    # Часовой пояс ЗАПРОСА, которым разбиралось относительное время этого
    # захвата. NULL = захват пришёл не из запроса с поясом (то есть из телеграма)
    # и разбирался поясом пространства.
    request_tz: str | None = None


@dataclass(frozen=True)
class TelegramAttachment:
    id: UUID
    user_space_id: UUID
    capture_event_id: UUID
    kind: CaptureSourceKind
    telegram_file_id: str = field(repr=False)
    telegram_file_unique_id: str = field(repr=False)
    # Голос несёт длительность (image → NULL); фото — размеры (voice → NULL).
    duration_seconds: int | None
    width: int | None
    height: int | None
    telegram_file_size: int | None
    telegram_mime_type: str | None
    storage_key: str | None = field(repr=False)
    sha256: str | None
    stored_size: int | None
    stored_mime_type: str | None
    stored_at: datetime | None
    created_at: datetime
    trace_id: str

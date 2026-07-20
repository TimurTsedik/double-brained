from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.capture.domain.entities import CaptureSourceKind


class CaptureEventModel(Base):
    __tablename__ = "capture_events"
    __table_args__ = (
        # Предикат ФОРМЫ строки, а не просто словарь каналов. Три телеграмных
        # идентификатора перестали быть NOT NULL — без этой замены журнал
        # молча потерял бы своё fail-closed свойство: строка channel='telegram'
        # с NULL-ами прошла бы вставку, а uq_capture_events_telegram_delivery
        # (NULLS DISTINCT) перестал бы ловить её повторы — то есть страховка от
        # двойной доставки выключилась бы ровно на тех строках, где ошиблись.
        # Обратный гибрид (channel='api' с телеграмными id) запрещён по той же
        # причине: иначе по строке нельзя сказать, чем она на самом деле
        # является. Один предикат, а не два: каждая ветка сама пришпиливает
        # channel, поэтому словарь каналов он уже содержит.
        CheckConstraint(
            "(channel = 'telegram'"
            "   AND bot_id IS NOT NULL"
            "   AND telegram_update_id IS NOT NULL"
            "   AND telegram_message_id IS NOT NULL"
            "   AND client_ref IS NULL"
            "   AND request_tz IS NULL)"
            " OR (channel = 'api'"
            "   AND bot_id IS NULL"
            "   AND telegram_update_id IS NULL"
            "   AND telegram_message_id IS NULL"
            "   AND client_ref IS NOT NULL AND client_ref <> ''"
            "   AND request_tz IS NOT NULL AND request_tz <> ''"
            "   AND source_kind = 'text')",
            name="ck_capture_events_channel",
        ),
        CheckConstraint(
            "modality IN ('text', 'voice_transcript')",
            name="ck_capture_events_modality",
        ),
        CheckConstraint(
            "(source_kind = 'text' AND raw_text IS NOT NULL AND raw_text <> '') "
            "OR (source_kind = 'voice' AND raw_text IS NULL) "
            # image: подпись хранится в raw_text как у текста, а без подписи —
            # NULL (записи нет, но журнал и файл сохранены).
            "OR (source_kind = 'image' AND (raw_text IS NULL OR raw_text <> ''))",
            name="ck_capture_events_kind_content",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_capture_events_trace_id",
        ),
        UniqueConstraint(
            "bot_id", "telegram_update_id", name="uq_capture_events_telegram_delivery"
        ),
        UniqueConstraint("id", "user_space_id", name="uq_capture_events_id_user_space"),
        UniqueConstraint(
            "id",
            "user_space_id",
            "source_kind",
            name="uq_capture_events_id_space_kind",
        ),
        # Ключ идемпотентности API-захвата. Частичный, потому что телеграмные
        # строки client_ref не несут вовсе, а UNIQUE по NULL'ам их бы не
        # различал. Последний рубеж, а не механизм: повтор распознаётся чтением
        # ДО вставки (см. эндпоинт), сюда доходит только гонка.
        Index(
            "uq_capture_events_client_ref",
            "user_space_id",
            "client_ref",
            unique=True,
            postgresql_where=text("client_ref IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    source_kind: Mapped[CaptureSourceKind] = mapped_column(
        Enum(
            CaptureSourceKind,
            name="capture_source_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
        server_default=CaptureSourceKind.TEXT.value,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_tz: Mapped[str | None] = mapped_column(Text, nullable=True)
    # server_default обязателен: create_voice/create_image ``modality`` не
    # передают, поэтому SQLAlchemy опускает колонку в INSERT'е — без умолчания
    # на стороне базы первый же голос или фото упёрлись бы в NOT NULL.
    modality: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="text"
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)


class TelegramAttachmentModel(Base):
    __tablename__ = "telegram_attachments"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_space_id", name="uq_telegram_attachments_id_user_space"
        ),
        UniqueConstraint(
            "capture_event_id",
            "user_space_id",
            name="uq_telegram_attachments_capture_space",
        ),
        ForeignKeyConstraint(
            ["capture_event_id", "user_space_id", "kind"],
            [
                "capture_events.id",
                "capture_events.user_space_id",
                "capture_events.source_kind",
            ],
            name="fk_telegram_attachments_capture_same_space_kind",
        ),
        CheckConstraint(
            "kind IN ('voice', 'image')", name="ck_telegram_attachments_kind"
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_telegram_attachments_duration",
        ),
        # Kind-условные поля: voice несёт длительность (размеров нет), image —
        # размеры (длительности нет). Составной FK уже гарантирует
        # kind = capture_events.source_kind.
        CheckConstraint(
            "(kind = 'voice' AND duration_seconds IS NOT NULL "
            "AND width IS NULL AND height IS NULL) OR "
            "(kind = 'image' AND width IS NOT NULL AND height IS NOT NULL "
            "AND duration_seconds IS NULL)",
            name="ck_telegram_attachments_kind_fields",
        ),
        CheckConstraint(
            "(width IS NULL OR width >= 0) AND (height IS NULL OR height >= 0)",
            name="ck_telegram_attachments_dimensions",
        ),
        CheckConstraint(
            "telegram_file_size IS NULL OR telegram_file_size >= 0",
            name="ck_telegram_attachments_telegram_size",
        ),
        CheckConstraint(
            "(storage_key IS NULL AND sha256 IS NULL AND stored_size IS NULL "
            "AND stored_mime_type IS NULL AND stored_at IS NULL) OR "
            "(storage_key IS NOT NULL AND sha256 IS NOT NULL "
            "AND stored_size IS NOT NULL AND stored_mime_type IS NOT NULL "
            "AND stored_at IS NOT NULL)",
            name="ck_telegram_attachments_storage_state",
        ),
        CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_telegram_attachments_sha256",
        ),
        CheckConstraint(
            "stored_size IS NULL OR stored_size >= 0",
            name="ck_telegram_attachments_stored_size",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_telegram_attachments_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[CaptureSourceKind] = mapped_column(
        Enum(
            CaptureSourceKind,
            name="telegram_attachment_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_file_unique_id: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    telegram_file_size: Mapped[int | None] = mapped_column(Integer)
    telegram_mime_type: Mapped[str | None] = mapped_column(String(255))
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    stored_size: Mapped[int | None] = mapped_column(Integer)
    stored_mime_type: Mapped[str | None] = mapped_column(String(255))
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)

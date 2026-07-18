"""ORM-модели weblinks: sidecar-ссылки записей и очередь титулов страниц."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.weblinks.domain.entities import (
    PageTitleStatus,
    WeblinkRecordKind,
)


class RecordUrlModel(Base):
    """Упорядоченные пары «слово → адрес» записи. Append-only sidecar:
    текст записи не трогается, у роли нет UPDATE/DELETE на эту таблицу."""

    __tablename__ = "record_urls"
    __table_args__ = (
        UniqueConstraint(
            "user_space_id",
            "record_kind",
            "record_id",
            "position",
            name="uq_record_urls_record_position",
        ),
        CheckConstraint("position >= 0", name="ck_record_urls_position"),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_record_urls_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    # Полиморфная привязка (вид + id фактической записи): FK на пять типовых
    # таблиц невозможен, изоляцию держат RLS и same-space предикаты чтения.
    record_kind: Mapped[WeblinkRecordKind] = mapped_column(
        Enum(
            WeblinkRecordKind,
            name="weblink_record_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        ),
        nullable=False,
    )
    record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class PageTitleModel(Base):
    """Очередь и кэш титулов: одна строка на (пространство, normalized_url).

    original_url — как прислан (фетч идёт по нему), normalized_url — ключ
    дедупликации. Модель попыток — по образцу reminders: attempt_count,
    next_attempt_at (бэкофф), потолок → failed.
    """

    __tablename__ = "page_titles"
    __table_args__ = (
        UniqueConstraint(
            "user_space_id",
            "normalized_url",
            name="uq_page_titles_space_normalized_url",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_page_titles_attempt_count"),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_page_titles_trace_id",
        ),
        # Скан «пора фетчить»: pending-строки по моменту следующей попытки.
        Index("ix_page_titles_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PageTitleStatus] = mapped_column(
        Enum(
            PageTitleStatus,
            name="page_title_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

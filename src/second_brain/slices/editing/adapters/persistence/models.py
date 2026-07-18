"""ORM-модель pending-режима правки записи (S3, спека §3.2)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.retrieval.application.contracts import SearchRecordType


class PendingEditModeModel(Base):
    """Одна строка на пространство: какая запись ждёт новый текст.

    Транзиентное UI-состояние по образцу pending_search_modes: строка живёт
    только пока ждём следующий текст; consume/отмена удаляют её (полный CRUD
    у роли приложения). Полиморфная привязка (вид + id записи) — FK на пять
    типовых таблиц невозможен, изоляцию держат forced RLS и повторная проверка
    владения при consume.
    """

    __tablename__ = "pending_edit_modes"
    __table_args__ = (
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_pending_edit_modes_trace_id",
        ),
    )

    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), primary_key=True
    )
    record_kind: Mapped[SearchRecordType] = mapped_column(
        Enum(
            SearchRecordType,
            name="edit_record_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        ),
        nullable=False,
    )
    record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

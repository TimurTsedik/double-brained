from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base

# Имя expression-индекса нужно upsert'у (ON CONFLICT по (user_space_id,
# lower(display_name))): повторная карточка с тем же именем обновляет номер.
CONTACT_NAME_UNIQUE_INDEX_NAME = "uq_contacts_space_lower_display_name"


class ContactModel(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_contacts_trace_id",
        ),
        Index(
            CONTACT_NAME_UNIQUE_INDEX_NAME,
            "user_space_id",
            text("lower(display_name)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    # PII: имя и номер видны только через RLS-скоуп своего пространства.
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

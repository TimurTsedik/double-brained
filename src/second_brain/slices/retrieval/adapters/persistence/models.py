from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base


class PendingSearchModeModel(Base):
    __tablename__ = "pending_search_modes"
    __table_args__ = (
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_pending_search_modes_trace_id",
        ),
    )

    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

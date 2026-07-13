from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.tasks.domain.entities import PendingCaptureType, TaskStatus


class TaskModel(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_tasks_id_user_space"),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_tasks_source_capture_event_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_tasks_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
    )
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class TaskProvenanceModel(Base):
    __tablename__ = "task_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "user_space_id"],
            ["tasks.id", "tasks.user_space_id"],
            name="fk_task_provenance_task_same_space",
        ),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_task_provenance_source_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_task_provenance_trace_id",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class PendingCaptureSelectionModel(Base):
    __tablename__ = "pending_capture_selections"
    __table_args__ = (
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_pending_capture_selections_trace_id",
        ),
    )

    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), primary_key=True
    )
    selection: Mapped[PendingCaptureType] = mapped_column(
        Enum(
            PendingCaptureType,
            name="pending_capture_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda modes: [mode.value for mode in modes],
        ),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

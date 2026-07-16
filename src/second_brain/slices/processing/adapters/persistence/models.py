from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.processing.domain.entities import (
    ProcessingNoticeKind,
    ProcessingNoticeStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)

TRACE_CHECK = "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)"
# Не-нативный Enum именует свой CHECK по имени типа (см. kind ниже); имя нужно
# реконсиляции init-db, когда живая база несёт старый набор kind'ов.
NOTICE_KIND_CHECK_NAME = "processing_notice_kind"


class ProcessingRunModel(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_space_id", name="uq_processing_runs_id_user_space"
        ),
        UniqueConstraint(
            "id",
            "capture_event_id",
            "user_space_id",
            name="uq_processing_runs_source_space",
        ),
        UniqueConstraint(
            "user_space_id",
            "capture_event_id",
            "version",
            name="uq_processing_runs_source_version",
        ),
        ForeignKeyConstraint(
            ["capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_processing_runs_capture_same_space",
        ),
        CheckConstraint("version >= 1", name="ck_processing_runs_version"),
        CheckConstraint(TRACE_CHECK, name="ck_processing_runs_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    output_type: Mapped[TranscriptionOutputType] = mapped_column(
        Enum(
            TranscriptionOutputType,
            name="transcription_output_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)


class ProcessingStepModel(Base):
    __tablename__ = "processing_steps"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_space_id", name="uq_processing_steps_id_user_space"
        ),
        UniqueConstraint(
            "processing_run_id",
            "step_type",
            name="uq_processing_steps_run_type",
        ),
        ForeignKeyConstraint(
            ["processing_run_id", "user_space_id"],
            ["processing_runs.id", "processing_runs.user_space_id"],
            name="fk_processing_steps_run_same_space",
        ),
        CheckConstraint("status BETWEEN 0 AND 5", name="ck_processing_steps_status"),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="ck_processing_steps_attempt_count",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code ~ '^[a-z0-9_]+$'",
            name="ck_processing_steps_safe_error_code",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_processing_steps_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    processing_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    step_type: Mapped[ProcessingStepType] = mapped_column(
        Enum(
            ProcessingStepType,
            name="processing_step_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)


class TranscriptModel(Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_transcripts_id_user_space"),
        UniqueConstraint(
            "user_space_id",
            "capture_event_id",
            "version",
            name="uq_transcripts_source_version",
        ),
        ForeignKeyConstraint(
            ["processing_run_id", "capture_event_id", "user_space_id"],
            [
                "processing_runs.id",
                "processing_runs.capture_event_id",
                "processing_runs.user_space_id",
            ],
            name="fk_transcripts_run_source_same_space",
        ),
        CheckConstraint("version >= 1", name="ck_transcripts_version"),
        CheckConstraint(
            "language_probability IS NULL OR language_probability BETWEEN 0 AND 1",
            name="ck_transcripts_language_probability",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_transcripts_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    language_probability: Mapped[float | None] = mapped_column(Float)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    segments: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)


class ProcessingNoticeModel(Base):
    __tablename__ = "processing_notices"
    __table_args__ = (
        UniqueConstraint(
            "processing_run_id", "kind", name="uq_processing_notices_run_kind"
        ),
        ForeignKeyConstraint(
            ["processing_run_id", "user_space_id"],
            ["processing_runs.id", "processing_runs.user_space_id"],
            name="fk_processing_notices_run_same_space",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="ck_processing_notices_attempt_count",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_processing_notices_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    processing_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[ProcessingNoticeKind] = mapped_column(
        Enum(
            ProcessingNoticeKind,
            name="processing_notice_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    status: Mapped[ProcessingNoticeStatus] = mapped_column(
        Enum(
            ProcessingNoticeStatus,
            name="processing_notice_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)

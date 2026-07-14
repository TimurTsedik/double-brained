from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base


class ClassificationResultModel(Base):
    __tablename__ = "classification_results"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_space_id", name="uq_classification_results_id_space"
        ),
        UniqueConstraint(
            "user_space_id",
            "processing_run_id",
            name="uq_classification_results_space_run",
        ),
        ForeignKeyConstraint(
            ["processing_run_id", "capture_event_id", "user_space_id"],
            [
                "processing_runs.id",
                "processing_runs.capture_event_id",
                "processing_runs.user_space_id",
            ],
            name="fk_classification_results_run_source_same_space",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_classification_results_source_sha256",
        ),
        CheckConstraint(
            "discarded_candidate_count >= 0",
            name="ck_classification_results_discarded_count",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_classification_results_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    processing_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidates: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    discarded_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)

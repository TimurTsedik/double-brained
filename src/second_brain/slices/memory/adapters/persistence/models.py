from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.memory.domain.entities import (
    EvidenceLevel,
    MemoryRecordKind,
    MemoryStepType,
)

TRACE_CHECK = "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)"


def _step_type_column() -> Mapped[MemoryStepType]:
    return mapped_column(
        Enum(
            MemoryStepType,
            name="memory_step_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )


def _record_kind_column() -> Mapped[MemoryRecordKind]:
    return mapped_column(
        Enum(
            MemoryRecordKind,
            name="memory_record_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )


class PendingMemoryQuestionModel(Base):
    __tablename__ = "pending_memory_questions"
    __table_args__ = (
        CheckConstraint(TRACE_CHECK, name="ck_pending_memory_questions_trace_id"),
    )

    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class MemoryQuestionModel(Base):
    __tablename__ = "memory_questions"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_space_id", name="uq_memory_questions_id_user_space"
        ),
        UniqueConstraint(
            "user_space_id",
            "bot_id",
            "telegram_update_id",
            name="uq_memory_questions_update_key",
        ),
        CheckConstraint("question_text <> ''", name="ck_memory_questions_text"),
        CheckConstraint(TRACE_CHECK, name="ck_memory_questions_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    current_project_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class MemoryAnswerRunModel(Base):
    __tablename__ = "memory_answer_runs"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_space_id", name="uq_memory_answer_runs_id_user_space"
        ),
        UniqueConstraint(
            "user_space_id", "question_id", name="uq_memory_answer_runs_question"
        ),
        ForeignKeyConstraint(
            ["question_id", "user_space_id"],
            ["memory_questions.id", "memory_questions.user_space_id"],
            name="fk_memory_answer_runs_question_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_memory_answer_runs_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    question_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class MemoryAnswerStepModel(Base):
    __tablename__ = "memory_answer_steps"
    __table_args__ = (
        UniqueConstraint(
            "user_space_id", "run_id", "step_type", name="uq_memory_answer_steps_run"
        ),
        ForeignKeyConstraint(
            ["run_id", "user_space_id"],
            ["memory_answer_runs.id", "memory_answer_runs.user_space_id"],
            name="fk_memory_answer_steps_run_same_space",
        ),
        CheckConstraint("status BETWEEN 0 AND 5", name="ck_memory_answer_steps_status"),
        CheckConstraint(
            "attempt_count >= 0", name="ck_memory_answer_steps_attempt_count"
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code ~ '^[a-z0-9_]{1,64}$'",
            name="ck_memory_answer_steps_safe_error_code",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    step_type: Mapped[MemoryStepType] = _step_type_column()
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MemoryRunEvidenceModel(Base):
    __tablename__ = "memory_run_evidence"
    __table_args__ = (
        UniqueConstraint(
            "user_space_id", "run_id", "label", name="uq_memory_run_evidence_label"
        ),
        ForeignKeyConstraint(
            ["run_id", "user_space_id"],
            ["memory_answer_runs.id", "memory_answer_runs.user_space_id"],
            name="fk_memory_run_evidence_run_same_space",
        ),
        CheckConstraint("label ~ '^S[0-9]+$'", name="ck_memory_run_evidence_label"),
        CheckConstraint("snippet_text <> ''", name="ck_memory_run_evidence_text"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    record_kind: Mapped[MemoryRecordKind] = _record_kind_column()
    record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    record_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    snippet_text: Mapped[str] = mapped_column(Text, nullable=False)


class MemoryAnswerModel(Base):
    __tablename__ = "memory_answers"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_memory_answers_id_user_space"),
        UniqueConstraint("user_space_id", "run_id", name="uq_memory_answers_run"),
        ForeignKeyConstraint(
            ["run_id", "user_space_id"],
            ["memory_answer_runs.id", "memory_answer_runs.user_space_id"],
            name="fk_memory_answers_run_same_space",
        ),
        CheckConstraint("answer_text <> ''", name="ck_memory_answers_text"),
        CheckConstraint(TRACE_CHECK, name="ck_memory_answers_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    evidence_level: Mapped[EvidenceLevel] = mapped_column(
        Enum(
            EvidenceLevel,
            name="memory_evidence_level",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class MemoryAnswerSourceModel(Base):
    __tablename__ = "memory_answer_sources"
    __table_args__ = (
        UniqueConstraint(
            "user_space_id", "answer_id", "label", name="uq_memory_answer_sources_label"
        ),
        ForeignKeyConstraint(
            ["answer_id", "user_space_id"],
            ["memory_answers.id", "memory_answers.user_space_id"],
            name="fk_memory_answer_sources_answer_same_space",
        ),
        ForeignKeyConstraint(
            ["user_space_id", "run_id", "label"],
            [
                "memory_run_evidence.user_space_id",
                "memory_run_evidence.run_id",
                "memory_run_evidence.label",
            ],
            name="fk_memory_answer_sources_snapshot",
        ),
        CheckConstraint("label <> ''", name="ck_memory_answer_sources_label"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    answer_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    record_kind: Mapped[MemoryRecordKind] = _record_kind_column()
    record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    record_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

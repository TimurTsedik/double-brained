from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base


class NoteModel(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_notes_id_user_space"),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_notes_source_capture_event_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_notes_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class NoteProvenanceModel(Base):
    __tablename__ = "note_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["note_id", "user_space_id"],
            ["notes.id", "notes.user_space_id"],
            name="fk_note_provenance_note_same_space",
        ),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_note_provenance_source_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_note_provenance_trace_id",
        ),
    )

    note_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class IdeaModel(Base):
    __tablename__ = "ideas"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_ideas_id_user_space"),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_ideas_source_capture_event_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_ideas_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class IdeaProvenanceModel(Base):
    __tablename__ = "idea_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["idea_id", "user_space_id"],
            ["ideas.id", "ideas.user_space_id"],
            name="fk_idea_provenance_idea_same_space",
        ),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_idea_provenance_source_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_idea_provenance_trace_id",
        ),
    )

    idea_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class DecisionModel(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_decisions_id_user_space"),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_decisions_source_capture_event_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_decisions_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class DecisionProvenanceModel(Base):
    __tablename__ = "decision_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["decision_id", "user_space_id"],
            ["decisions.id", "decisions.user_space_id"],
            name="fk_decision_provenance_decision_same_space",
        ),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_decision_provenance_source_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_decision_provenance_trace_id",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class QuestionModel(Base):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_questions_id_user_space"),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_questions_source_capture_event_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_questions_trace_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class QuestionProvenanceModel(Base):
    __tablename__ = "question_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["question_id", "user_space_id"],
            ["questions.id", "questions.user_space_id"],
            name="fk_question_provenance_question_same_space",
        ),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_question_provenance_source_same_space",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_question_provenance_trace_id",
        ),
    )

    question_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

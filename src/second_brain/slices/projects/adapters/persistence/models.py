from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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

TRACE_CHECK = "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)"


class ProjectModel(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("id", "user_space_id", name="uq_projects_id_user_space"),
        UniqueConstraint(
            "user_space_id", "name_key", name="uq_projects_user_space_name_key"
        ),
        CheckConstraint("name <> ''", name="ck_projects_name_not_empty"),
        CheckConstraint("name_key <> ''", name="ck_projects_name_key_not_empty"),
        CheckConstraint(TRACE_CHECK, name="ck_projects_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectContextModel(Base):
    __tablename__ = "project_contexts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["current_project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_contexts_current_project_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_contexts_trace_id"),
    )

    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), primary_key=True
    )
    current_project_id: Mapped[UUID | None] = mapped_column(Uuid)
    awaiting_name: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectCaptureEventLinkModel(Base):
    __tablename__ = "project_capture_event_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_capture_links_project_same_space",
        ),
        ForeignKeyConstraint(
            ["capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_project_capture_links_capture_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_capture_links_trace_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    capture_event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectNoteLinkModel(Base):
    __tablename__ = "project_note_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_note_links_project_same_space",
        ),
        ForeignKeyConstraint(
            ["note_id", "user_space_id"],
            ["notes.id", "notes.user_space_id"],
            name="fk_project_note_links_note_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_note_links_trace_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    note_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectTaskLinkModel(Base):
    __tablename__ = "project_task_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_task_links_project_same_space",
        ),
        ForeignKeyConstraint(
            ["task_id", "user_space_id"],
            ["tasks.id", "tasks.user_space_id"],
            name="fk_project_task_links_task_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_task_links_trace_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectIdeaLinkModel(Base):
    __tablename__ = "project_idea_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_idea_links_project_same_space",
        ),
        ForeignKeyConstraint(
            ["idea_id", "user_space_id"],
            ["ideas.id", "ideas.user_space_id"],
            name="fk_project_idea_links_idea_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_idea_links_trace_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    idea_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectDecisionLinkModel(Base):
    __tablename__ = "project_decision_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_decision_links_project_same_space",
        ),
        ForeignKeyConstraint(
            ["decision_id", "user_space_id"],
            ["decisions.id", "decisions.user_space_id"],
            name="fk_project_decision_links_decision_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_decision_links_trace_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    decision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectQuestionLinkModel(Base):
    __tablename__ = "project_question_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_space_id"],
            ["projects.id", "projects.user_space_id"],
            name="fk_project_question_links_project_same_space",
        ),
        ForeignKeyConstraint(
            ["question_id", "user_space_id"],
            ["questions.id", "questions.user_space_id"],
            name="fk_project_question_links_question_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_project_question_links_trace_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    question_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

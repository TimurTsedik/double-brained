from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.retrieval.application.contracts import EMBEDDING_DIMENSIONS
from second_brain.slices.retrieval.domain.entities import SearchRecordType

TRACE_CHECK = "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)"


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


class SemanticDocumentModel(Base):
    __tablename__ = "semantic_documents"
    __table_args__ = (
        UniqueConstraint(
            "user_space_id",
            "source_kind",
            "source_record_id",
            "index_version",
            "chunk_number",
            name="uq_semantic_documents_chunk",
        ),
        ForeignKeyConstraint(
            ["source_capture_event_id", "user_space_id"],
            ["capture_events.id", "capture_events.user_space_id"],
            name="fk_semantic_documents_capture_same_space",
        ),
        CheckConstraint("chunk_number >= 0", name="ck_semantic_documents_chunk_number"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_documents_content_sha256",
        ),
        CheckConstraint("chunk_text <> ''", name="ck_semantic_documents_chunk_text"),
        CheckConstraint(
            "index_version >= 1", name="ck_semantic_documents_index_version"
        ),
        CheckConstraint(TRACE_CHECK, name="ck_semantic_documents_trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    source_kind: Mapped[SearchRecordType] = mapped_column(
        Enum(
            SearchRecordType,
            name="semantic_document_source_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    source_record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_capture_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    index_version: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class IndexingTargetModel(Base):
    __tablename__ = "indexing_targets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["processing_run_id", "user_space_id"],
            ["processing_runs.id", "processing_runs.user_space_id"],
            name="fk_indexing_targets_run_same_space",
        ),
        CheckConstraint(TRACE_CHECK, name="ck_indexing_targets_trace_id"),
    )

    processing_run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_space_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_spaces.id"), nullable=False
    )
    record_kind: Mapped[SearchRecordType] = mapped_column(
        Enum(
            SearchRecordType,
            name="indexing_target_record_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)

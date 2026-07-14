"""Hybrid memory retrieval: exact full-text search plus exact pgvector cosine
search under one AccessContext, fused with deterministic reciprocal-rank
fusion (k=60, exact Fraction arithmetic), deduplicated by canonical record and
by source, bounded to at most MAX_CHUNKS chunks and MAX_CHARS characters."""

from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from uuid import UUID

from second_brain.slices.retrieval.application.contracts import (
    RetrieveMemoryCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    EvidenceBundle,
    EvidenceChunk,
    SearchRecord,
    SearchRecordType,
    SemanticMatch,
)
from second_brain.slices.retrieval.ports.embedding import EmbeddingModel
from second_brain.slices.retrieval.ports.repositories import (
    ExactSearchStore,
    SemanticIndexStore,
)

FTS_CANDIDATES = 24
VECTOR_CANDIDATES = 24
RRF_K = 60
MAX_CHUNKS = 12
MAX_CHARS = 12_000


class HybridMemoryRetrieval:
    """MemoryRetrievalPort implementation over the slice's exact stores."""

    def __init__(
        self,
        fts_store: ExactSearchStore,
        semantic_store: SemanticIndexStore,
        embedding_model: EmbeddingModel,
    ) -> None:
        self._fts_store = fts_store
        self._semantic_store = semantic_store
        self._embedding_model = embedding_model

    async def retrieve(self, command: RetrieveMemoryCommand) -> EvidenceBundle:
        question = " ".join(command.question.split())
        if not question:
            return EvidenceBundle(
                chunks=(), current_project_id=command.current_project_id
            )
        records = await self._fts_store.search(
            command.access_context, question, FTS_CANDIDATES
        )
        query_vector = await self._embedding_model.embed_query(question)
        matches = await self._semantic_store.search_similar(
            command.access_context, query_vector, VECTOR_CANDIDATES
        )
        return EvidenceBundle(
            chunks=_fuse(records, matches),
            current_project_id=command.current_project_id,
        )


@dataclass
class _Candidate:
    kind: SearchRecordType
    record_id: UUID
    created_at: datetime
    score: Fraction = Fraction(0)
    fts_record: SearchRecord | None = None
    matches: list[SemanticMatch] = field(default_factory=list)


def _fuse(
    records: tuple[SearchRecord, ...],
    matches: tuple[SemanticMatch, ...],
) -> tuple[EvidenceChunk, ...]:
    candidates: dict[tuple[SearchRecordType, UUID], _Candidate] = {}
    for rank, record in enumerate(records, start=1):
        key = (record.record_type, record.id)
        candidate = candidates.get(key)
        if candidate is None:
            candidate = _Candidate(
                kind=record.record_type,
                record_id=record.id,
                created_at=record.created_at,
            )
            candidates[key] = candidate
        if candidate.fts_record is None:
            candidate.fts_record = record
            candidate.score += Fraction(1, RRF_K + rank)
    for rank, match in enumerate(matches, start=1):
        key = (match.record_kind, match.record_id)
        candidate = candidates.get(key)
        if candidate is None:
            # The canonical record date comes from the FTS row when present;
            # a vector-only candidate uses its best (first) chunk's date.
            candidate = _Candidate(
                kind=match.record_kind,
                record_id=match.record_id,
                created_at=match.created_at,
            )
            candidates[key] = candidate
        candidate.score += Fraction(1, RRF_K + rank)
        candidate.matches.append(match)
    # Full determinism: score desc, created_at desc, kind asc, id asc —
    # applied as three stable sorts from the least significant key up.
    ordered = sorted(
        candidates.values(), key=lambda candidate: (candidate.kind, candidate.record_id)
    )
    ordered.sort(key=lambda candidate: candidate.created_at, reverse=True)
    ordered.sort(key=lambda candidate: candidate.score, reverse=True)
    return _emit(ordered)


def _candidate_chunks(candidate: _Candidate) -> tuple[EvidenceChunk, ...]:
    if candidate.matches:
        # Dedup rule (a): a record with vector chunks keeps only them in
        # vector-rank order; its FTS hit adds no pseudo-chunk.
        return tuple(
            EvidenceChunk(
                record_kind=candidate.kind,
                record_id=candidate.record_id,
                source_capture_event_id=match.source_capture_event_id,
                chunk_number=match.chunk_number,
                text=match.text,
                created_at=match.created_at,
            )
            for match in candidate.matches
        )
    record = candidate.fts_record
    if record is None:  # Unreachable: every candidate is born from a path.
        return ()
    return (
        EvidenceChunk(
            record_kind=candidate.kind,
            record_id=candidate.record_id,
            source_capture_event_id=record.source_capture_event_id,
            chunk_number=None,
            text=record.text,
            created_at=record.created_at,
        ),
    )


def _emit(ordered: list[_Candidate]) -> tuple[EvidenceChunk, ...]:
    emitted: list[EvidenceChunk] = []
    seen_source_texts: set[tuple[UUID, str]] = set()
    total_chars = 0
    for candidate in ordered:
        for chunk in _candidate_chunks(candidate):
            source_text = (chunk.source_capture_event_id, chunk.text)
            if source_text in seen_source_texts:
                # Dedup rule (b): sibling duplicate of an already emitted
                # source text is skipped, it never stops the emission.
                continue
            if len(emitted) >= MAX_CHUNKS:
                return tuple(emitted)
            if total_chars + len(chunk.text) > MAX_CHARS:
                # Bound: emission stops BEFORE the overflowing chunk.
                return tuple(emitted)
            seen_source_texts.add(source_text)
            emitted.append(chunk)
            total_chars += len(chunk.text)
    return tuple(emitted)

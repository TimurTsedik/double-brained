from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CandidateType(StrEnum):
    NOTE = "note"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    QUESTION = "question"


class CandidateModality(StrEnum):
    COMMITMENT = "commitment"
    SUGGESTION = "suggestion"
    HYPOTHESIS = "hypothesis"
    COMPLETED_ACTION = "completed_action"
    QUESTION = "question"
    DECISION = "decision"
    OBSERVATION = "observation"


ALLOWED_MODALITIES_BY_TYPE = {
    CandidateType.NOTE: (
        CandidateModality.OBSERVATION,
        CandidateModality.COMPLETED_ACTION,
    ),
    CandidateType.TASK: (CandidateModality.COMMITMENT,),
    CandidateType.IDEA: (
        CandidateModality.SUGGESTION,
        CandidateModality.HYPOTHESIS,
    ),
    CandidateType.DECISION: (CandidateModality.DECISION,),
    CandidateType.QUESTION: (CandidateModality.QUESTION,),
}


class CandidateDisposition(StrEnum):
    MATERIALIZE = "materialize"
    NEEDS_REVIEW = "needs_review"
    ALREADY_CAPTURED = "already_captured"


class CandidateStorageStatus(StrEnum):
    MATERIALIZED = "materialized"
    NEEDS_REVIEW = "needs_review"
    ALREADY_CAPTURED = "already_captured"


class CandidateValidationCode(StrEnum):
    VALID = "valid"
    QUOTE_NOT_FOUND = "quote_not_found"
    INVALID_CONFIDENCE = "invalid_confidence"
    TYPE_MODALITY_MISMATCH = "type_modality_mismatch"
    LOW_CONFIDENCE = "low_confidence"
    BASE_NOTE_FRAGMENT = "base_note_fragment"
    ALREADY_CAPTURED = "already_captured"


@dataclass(frozen=True, slots=True)
class StoredCandidate:
    candidate_type: CandidateType
    source_quote: str = field(repr=False)
    modality: CandidateModality
    confidence: float | None
    status: CandidateStorageStatus
    validation_code: CandidateValidationCode
    materialized_record_id: UUID | None


@dataclass(frozen=True, slots=True)
class ClassificationCandidateDraft:
    candidate_type: CandidateType
    source_quote: str = field(repr=False)
    modality: CandidateModality
    confidence: float


@dataclass(frozen=True, slots=True)
class GroundedCandidate:
    candidate_type: CandidateType
    source_quote: str = field(repr=False)
    modality: CandidateModality
    confidence: float | None
    disposition: CandidateDisposition
    validation_code: CandidateValidationCode


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    id: UUID
    user_space_id: UUID
    processing_run_id: UUID
    capture_event_id: UUID
    source_sha256: str
    model_name: str
    prompt_version: str
    schema_version: str
    candidates: tuple[StoredCandidate, ...] = field(repr=False)
    discarded_candidate_count: int
    created_at: datetime
    trace_id: str

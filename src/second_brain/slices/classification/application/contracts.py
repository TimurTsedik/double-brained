from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.classification.domain.entities import (
    CandidateType,
    ClassificationCandidateDraft,
    GroundedCandidate,
    StoredCandidate,
)
from second_brain.slices.identity.application.contracts import AccessContext


@dataclass(frozen=True, slots=True)
class ClassificationSource:
    text: str = field(repr=False)
    base_type: CandidateType


@dataclass(frozen=True, slots=True)
class ClassificationRequest:
    source_text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ClassificationDraft:
    model_name: str
    prompt_version: str
    schema_version: str
    candidates: tuple[ClassificationCandidateDraft, ...] = field(repr=False)
    discarded_candidate_count: int


@dataclass(frozen=True, slots=True)
class ClassificationOutcome:
    source_sha256: str
    model_name: str | None
    prompt_version: str | None
    schema_version: str | None
    candidates: tuple[GroundedCandidate, ...] = field(repr=False)
    discarded_candidate_count: int
    skipped_reason: str | None


@dataclass(frozen=True, slots=True)
class CompleteClassificationCommand:
    access_context: AccessContext
    step_id: UUID
    outcome: ClassificationOutcome = field(repr=False)
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class StoreClassificationResultCommand:
    access_context: AccessContext
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


@dataclass(frozen=True, slots=True)
class ReadClassificationSourceCommand:
    access_context: AccessContext
    processing_run_id: UUID
    capture_event_id: UUID
    base_type: CandidateType


class ClassificationSourcePort(Protocol):
    async def read(
        self, command: ReadClassificationSourceCommand
    ) -> ClassificationSource: ...


class ClassificationCompletionPort(Protocol):
    async def complete(self, command: CompleteClassificationCommand) -> None: ...

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.domain.entities import (
    ProcessingNoticeClaim,
    TranscriptionOutputType,
    TranscriptSegment,
)


@dataclass(frozen=True)
class TranscriptionDraft:
    text: str = field(repr=False)
    language: str
    language_probability: float | None
    model_name: str
    segments: tuple[TranscriptSegment, ...] = field(repr=False)


@dataclass(frozen=True)
class CreateVoiceProcessingRunCommand:
    access_context: AccessContext
    capture_event_id: UUID
    output_type: TranscriptionOutputType
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class SucceedProcessingStepCommand:
    access_context: AccessContext
    step_id: UUID
    completed_at: datetime


@dataclass(frozen=True)
class FailProcessingStepCommand:
    access_context: AccessContext
    step_id: UUID
    failed_at: datetime
    safe_error_code: str


@dataclass(frozen=True)
class StoreVoiceCommand:
    access_context: AccessContext
    capture_event_id: UUID
    content: bytes = field(repr=False)
    mime_type: str | None


@dataclass(frozen=True)
class StoredVoice:
    storage_key: str = field(repr=False)
    local_path: str = field(repr=False)
    sha256: str
    size: int
    mime_type: str


@dataclass(frozen=True)
class TranscribeVoiceCommand:
    local_path: str = field(repr=False)


@dataclass(frozen=True)
class LocateVoiceCommand:
    access_context: AccessContext
    capture_event_id: UUID


@dataclass(frozen=True)
class StoredVoiceLocation:
    local_path: str = field(repr=False)


@dataclass(frozen=True)
class DownloadVoiceCommand:
    file_id: str = field(repr=False)
    mime_type: str | None


@dataclass(frozen=True)
class DownloadedVoice:
    content: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class CompleteVoiceDownloadCommand:
    access_context: AccessContext
    step_id: UUID
    capture_event_id: UUID
    stored_voice: StoredVoice = field(repr=False)
    completed_at: datetime


@dataclass(frozen=True)
class CompleteVoiceTranscriptionCommand:
    access_context: AccessContext
    step_id: UUID
    draft: TranscriptionDraft = field(repr=False)
    completed_at: datetime


@dataclass(frozen=True)
class MarkProcessingNoticeSentCommand:
    access_context: AccessContext
    notice_id: UUID
    sent_at: datetime


@dataclass(frozen=True)
class SendProcessingNoticeCommand:
    recipient_telegram_id: int = field(repr=False)
    notice: ProcessingNoticeClaim

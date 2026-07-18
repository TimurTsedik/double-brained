from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from second_brain.shared.i18n import Locale
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
    # Тип заморожен ДЕФОЛТНО (кнопку не нажимали) → при расшифровке со временем
    # маршрутизируется в задачу. Явно выбранный тип — False.
    route_default_by_time: bool = False


@dataclass(frozen=True)
class CreateTextProcessingRunCommand:
    access_context: AccessContext
    capture_event_id: UUID
    output_type: TranscriptionOutputType
    created_at: datetime
    trace_id: str
    # Текст материализуется синхронно по факту → тип уже финальный, доп.
    # маршрутизация на завершении не нужна.
    route_default_by_time: bool = False


@dataclass(frozen=True)
class CreateImageProcessingRunCommand:
    """Прогон обработки фото — ОДИН на capture.

    С подписью запись уже создана СИНХРОННО (тип финальный) → output_type задан,
    шаги IMAGE_DOWNLOAD + CLASSIFICATION + INDEXING (классификация/индексация НЕ
    гейтятся download'ом: текст подписи независим от байтов). Без подписи —
    source-only прогон: output_type None, единственный шаг IMAGE_DOWNLOAD.
    """

    access_context: AccessContext
    capture_event_id: UUID
    output_type: TranscriptionOutputType | None
    created_at: datetime
    trace_id: str
    # Тип у фото всегда финальный на момент создания прогона (запись синхронна).
    route_default_by_time: bool = False


@dataclass(frozen=True)
class CreateIndexProcessingRunCommand:
    """Прогон пере-индексации после правки записи (S3, спека §3.3).

    ЕДИНСТВЕННЫЙ шаг — INDEXING: никакой классификации и извлечения времени
    (create_text_run завёл бы CLASSIFICATION заново → классификатор снова
    материализовал бы кандидатов = дубли записей и повторный reminder-путь).
    Источник текста для шага — сама правленая типизированная запись (её и
    читает indexing-source), а не CaptureEvent. Версия прогона выделяется
    автоматически: max(version)+1 по (пространство, capture) — уникальность
    uq_processing_runs_source_version соблюдена без изменения старых прогонов.
    """

    access_context: AccessContext
    capture_event_id: UUID
    output_type: TranscriptionOutputType
    created_at: datetime
    trace_id: str
    route_default_by_time: bool = False


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
class SkipProcessingStepCommand:
    access_context: AccessContext
    step_id: UUID
    skipped_at: datetime
    safe_reason_code: str


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
class StoreImageCommand:
    # Mime Telegram у фото не отдаёт: хранилище sniff'ит байты само.
    access_context: AccessContext
    capture_event_id: UUID
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class StoredImage:
    storage_key: str = field(repr=False)
    local_path: str = field(repr=False)
    sha256: str
    size: int
    mime_type: str


@dataclass(frozen=True)
class DownloadImageCommand:
    file_id: str = field(repr=False)


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class CompleteVoiceDownloadCommand:
    access_context: AccessContext
    step_id: UUID
    capture_event_id: UUID
    stored_voice: StoredVoice = field(repr=False)
    completed_at: datetime


@dataclass(frozen=True)
class CompleteImageDownloadCommand:
    access_context: AccessContext
    step_id: UUID
    capture_event_id: UUID
    stored_image: StoredImage = field(repr=False)
    completed_at: datetime


@dataclass(frozen=True)
class CompleteVoiceTranscriptionCommand:
    access_context: AccessContext
    step_id: UUID
    draft: TranscriptionDraft = field(repr=False)
    completed_at: datetime
    # Фактический тип записи после материализации (голос со временем мог стать
    # задачей). Проставляет bootstrap ПОСЛЕ create_for_selection; идёт в метку
    # уведомления. None → метку берём из замороженного типа прогона.
    resolved_output_type: TranscriptionOutputType | None = None


@dataclass(frozen=True)
class MarkProcessingNoticeSentCommand:
    access_context: AccessContext
    notice_id: UUID
    sent_at: datetime


@dataclass(frozen=True)
class SendProcessingNoticeCommand:
    recipient_telegram_id: int = field(repr=False)
    notice: ProcessingNoticeClaim
    locale: Locale

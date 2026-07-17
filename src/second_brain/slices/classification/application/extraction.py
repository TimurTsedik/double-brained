import hashlib
import math

from second_brain.shared.secret_scan import contains_credential
from second_brain.slices.classification.application.contracts import (
    ClassificationOutcome,
    ClassificationRequest,
    ClassificationSource,
)
from second_brain.slices.classification.domain.entities import (
    ALLOWED_MODALITIES_BY_TYPE,
    CandidateDisposition,
    CandidateType,
    CandidateValidationCode,
    ClassificationCandidateDraft,
    GroundedCandidate,
)
from second_brain.slices.classification.ports.model import ClassificationModel

MAX_CANDIDATES = 8
MATERIALIZATION_CONFIDENCE = 0.90
CREDENTIAL_DETECTED = "credential_detected"

# Кандидат считается «весь текст» с точностью до окружающих пробелов и финальной
# пунктуации: LLM часто цитирует сообщение без завершающей точки/«?»/«!». Такой
# кандидат — дубль базовой записи, а не отдельный под-пункт.
_WHOLE_SOURCE_EDGE = " \t\n\r.,;:!?…"


def _covers_whole_source(quote: str, text: str) -> bool:
    return quote.strip(_WHOLE_SOURCE_EDGE) == text.strip(_WHOLE_SOURCE_EDGE)


class ClassifySource:
    def __init__(self, model: ClassificationModel) -> None:
        self._model = model

    async def execute(self, source: ClassificationSource) -> ClassificationOutcome:
        source_sha256 = hashlib.sha256(source.text.encode("utf-8")).hexdigest()
        if contains_credential(source.text):
            return ClassificationOutcome(
                source_sha256=source_sha256,
                model_name=None,
                prompt_version=None,
                schema_version=None,
                candidates=(),
                discarded_candidate_count=0,
                skipped_reason=CREDENTIAL_DETECTED,
            )

        draft = await self._model.classify(
            ClassificationRequest(source_text=source.text)
        )
        candidates, discarded = _validate_candidates(source, draft.candidates)
        return ClassificationOutcome(
            source_sha256=source_sha256,
            model_name=draft.model_name,
            prompt_version=draft.prompt_version,
            schema_version=draft.schema_version,
            candidates=candidates,
            discarded_candidate_count=(draft.discarded_candidate_count + discarded),
            skipped_reason=None,
        )


def _validate_candidates(
    source: ClassificationSource,
    drafts: tuple[ClassificationCandidateDraft, ...],
) -> tuple[tuple[GroundedCandidate, ...], int]:
    candidates: list[GroundedCandidate] = []
    seen: set[tuple[CandidateType, str]] = set()
    discarded = 0
    for draft in drafts:
        key = (draft.candidate_type, draft.source_quote)
        if key in seen:
            discarded += 1
            continue
        seen.add(key)
        if len(candidates) >= MAX_CANDIDATES:
            discarded += 1
            continue
        candidates.append(_validate_candidate(source, draft))
    return tuple(candidates), discarded


def _validate_candidate(
    source: ClassificationSource,
    draft: ClassificationCandidateDraft,
) -> GroundedCandidate:
    confidence = draft.confidence if math.isfinite(draft.confidence) else None
    if not draft.source_quote or draft.source_quote not in source.text:
        return _review(draft, confidence, CandidateValidationCode.QUOTE_NOT_FOUND)
    if confidence is None or not 0 <= confidence <= 1:
        return _review(
            draft,
            None,
            CandidateValidationCode.INVALID_CONFIDENCE,
        )
    if draft.modality not in ALLOWED_MODALITIES_BY_TYPE[draft.candidate_type]:
        return _review(
            draft,
            confidence,
            CandidateValidationCode.TYPE_MODALITY_MISMATCH,
        )
    if confidence < MATERIALIZATION_CONFIDENCE:
        return _review(draft, confidence, CandidateValidationCode.LOW_CONFIDENCE)
    if _covers_whole_source(draft.source_quote, source.text):
        # Весь текст уже материализован базовой записью на capture (её тип задан
        # кнопкой или временем). Кандидат, покрывающий ВЕСЬ текст, — это не
        # вторая сущность, а дубль той же записи (даже если ИИ выбрал другой
        # тип): не материализуем. Классификатор порождает записи только для
        # ОТДЕЛЬНЫХ под-пунктов (частичная цитата), а не для целого сообщения.
        return GroundedCandidate(
            candidate_type=draft.candidate_type,
            source_quote=draft.source_quote,
            modality=draft.modality,
            confidence=confidence,
            disposition=CandidateDisposition.ALREADY_CAPTURED,
            validation_code=CandidateValidationCode.ALREADY_CAPTURED,
        )
    if (
        source.base_type is CandidateType.NOTE
        and draft.candidate_type is CandidateType.NOTE
    ):
        return _review(
            draft,
            confidence,
            CandidateValidationCode.BASE_NOTE_FRAGMENT,
        )
    return GroundedCandidate(
        candidate_type=draft.candidate_type,
        source_quote=draft.source_quote,
        modality=draft.modality,
        confidence=confidence,
        disposition=CandidateDisposition.MATERIALIZE,
        validation_code=CandidateValidationCode.VALID,
    )


def _review(
    draft: ClassificationCandidateDraft,
    confidence: float | None,
    validation_code: CandidateValidationCode,
) -> GroundedCandidate:
    return GroundedCandidate(
        candidate_type=draft.candidate_type,
        source_quote=draft.source_quote,
        modality=draft.modality,
        confidence=confidence,
        disposition=CandidateDisposition.NEEDS_REVIEW,
        validation_code=validation_code,
    )

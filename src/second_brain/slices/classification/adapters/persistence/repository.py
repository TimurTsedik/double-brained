from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.classification.adapters.persistence.models import (
    ClassificationResultModel,
)
from second_brain.slices.classification.application.contracts import (
    StoreClassificationResultCommand,
)
from second_brain.slices.classification.domain.entities import (
    CandidateModality,
    CandidateStorageStatus,
    CandidateType,
    CandidateValidationCode,
    ClassificationResult,
    StoredCandidate,
)
from second_brain.slices.identity.application.contracts import AccessContext


class PostgresClassificationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_result(
        self, access_context: AccessContext, processing_run_id: UUID
    ) -> ClassificationResult | None:
        async with self._session_factory() as session:
            async with session.begin():
                await _set_user_space_scope(session, access_context)
                model = await session.scalar(
                    select(ClassificationResultModel).where(
                        ClassificationResultModel.processing_run_id
                        == processing_run_id,
                        ClassificationResultModel.user_space_id
                        == access_context.user_space_id,
                    )
                )
                return None if model is None else _to_entity(model)

    async def count_results(self, access_context: AccessContext) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                await _set_user_space_scope(session, access_context)
                count = await session.scalar(
                    select(func.count())
                    .select_from(ClassificationResultModel)
                    .where(
                        ClassificationResultModel.user_space_id
                        == access_context.user_space_id
                    )
                )
                return int(count or 0)


class PostgresClassificationWriter:
    """Writes a classification result in a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, command: StoreClassificationResultCommand
    ) -> ClassificationResult:
        await _set_user_space_scope(self._session, command.access_context)
        model = ClassificationResultModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            processing_run_id=command.processing_run_id,
            capture_event_id=command.capture_event_id,
            source_sha256=command.source_sha256,
            model_name=command.model_name,
            prompt_version=command.prompt_version,
            schema_version=command.schema_version,
            candidates=[_candidate_json(candidate) for candidate in command.candidates],
            discarded_candidate_count=command.discarded_candidate_count,
            created_at=command.created_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_entity(model)


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_entity(model: ClassificationResultModel) -> ClassificationResult:
    return ClassificationResult(
        id=model.id,
        user_space_id=model.user_space_id,
        processing_run_id=model.processing_run_id,
        capture_event_id=model.capture_event_id,
        source_sha256=model.source_sha256,
        model_name=model.model_name,
        prompt_version=model.prompt_version,
        schema_version=model.schema_version,
        candidates=tuple(
            _stored_candidate(candidate) for candidate in model.candidates
        ),
        discarded_candidate_count=model.discarded_candidate_count,
        created_at=model.created_at,
        trace_id=model.trace_id,
    )


def _candidate_json(candidate: StoredCandidate) -> dict[str, object]:
    return {
        "type": candidate.candidate_type.value,
        "source_quote": candidate.source_quote,
        "modality": candidate.modality.value,
        "confidence": candidate.confidence,
        "status": candidate.status.value,
        "validation_code": candidate.validation_code.value,
        "materialized_record_id": (
            None
            if candidate.materialized_record_id is None
            else str(candidate.materialized_record_id)
        ),
    }


def _stored_candidate(value: dict[str, object]) -> StoredCandidate:
    confidence = value["confidence"]
    record_id = value["materialized_record_id"]
    return StoredCandidate(
        candidate_type=CandidateType(str(value["type"])),
        source_quote=str(value["source_quote"]),
        modality=CandidateModality(str(value["modality"])),
        confidence=None if confidence is None else float(str(confidence)),
        status=CandidateStorageStatus(str(value["status"])),
        validation_code=CandidateValidationCode(str(value["validation_code"])),
        materialized_record_id=None if record_id is None else UUID(str(record_id)),
    )

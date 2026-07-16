from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.classification_completion import (
    ClassificationCompletionInTransaction,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.classification.adapters.persistence.models import (
    ClassificationResultModel,
)
from second_brain.slices.classification.adapters.persistence.repository import (
    PostgresClassificationRepository,
)
from second_brain.slices.classification.application.contracts import (
    ClassificationOutcome,
    CompleteClassificationCommand,
)
from second_brain.slices.classification.domain.entities import (
    CandidateDisposition,
    CandidateModality,
    CandidateType,
    CandidateValidationCode,
    GroundedCandidate,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingStepModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CreateTextProcessingRunCommand,
    FailProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.tasks.adapters.persistence.models import (
    TaskModel,
    TaskProvenanceModel,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
ACCESS_A = AccessContext(
    UUID("10000000-0000-0000-0000-000000000001"),
    UUID("10000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("20000000-0000-0000-0000-000000000002"),
    UUID("20000000-0000-0000-0000-000000000012"),
)


class SpyConfirmationDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []

    async def deliver(self, text: str, recipient: TelegramRecipient) -> None:
        self.sent.append((text, recipient.telegram_user_id))


class FixedWorkerIdentity:
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        return (ACCESS_A,)

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        return TelegramRecipient(telegram_user_id=42)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        return Locale.RU


def _completion(
    engine: AsyncEngine, spy: SpyConfirmationDelivery | None = None
) -> ClassificationCompletionInTransaction:
    return ClassificationCompletionInTransaction(
        create_session_factory(engine),
        spy or SpyConfirmationDelivery(),
        FixedWorkerIdentity(),
    )


@pytest_asyncio.fixture(autouse=True)
async def reset_classification_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                _user(ACCESS_A),
                _user(ACCESS_B),
            ],
        )
        await connection.execute(
            insert(UserSpace),
            [
                _space(ACCESS_A),
                _space(ACCESS_B),
            ],
        )


def _user(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_id,
        "role": "member",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _space(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_space_id,
        "owner_user_id": access.user_id,
        "timezone": "Asia/Jerusalem",
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }


async def _claimed_text_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
    output_type: TranscriptionOutputType = TranscriptionOutputType.NOTE,
) -> tuple[PostgresProcessingRepository, UUID, UUID, UUID]:
    capture_event_id = uuid4()
    trace_id = f"{update_id:x}".rjust(32, "a")[-32:]
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_event_id,
                user_space_id=access.user_space_id,
                source_kind="text",
                channel="telegram",
                bot_id=10,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 1_000,
                raw_text="Надо позвонить Сергею. Использовать Qdrant?",
                received_at=NOW,
                created_at=NOW,
                trace_id=trace_id,
            )
        )
    repository = PostgresProcessingRepository(create_session_factory(engine))
    run = await repository.create_text_run(
        CreateTextProcessingRunCommand(
            access_context=access,
            capture_event_id=capture_event_id,
            output_type=output_type,
            created_at=NOW,
            trace_id=trace_id,
        )
    )
    claim = await repository.claim_due_step(
        access,
        NOW,
        LEASE,
        (ProcessingStepType.CLASSIFICATION,),
    )
    assert claim is not None
    return repository, capture_event_id, run.id, claim.step_id


def _candidate(
    candidate_type: CandidateType,
    quote: str,
    *,
    disposition: CandidateDisposition,
    validation_code: CandidateValidationCode,
    modality: CandidateModality,
    confidence: float | None = 0.95,
) -> GroundedCandidate:
    return GroundedCandidate(
        candidate_type=candidate_type,
        source_quote=quote,
        modality=modality,
        confidence=confidence,
        disposition=disposition,
        validation_code=validation_code,
    )


def _outcome(
    *,
    model_name: str = "qwen3:4b",
    task_quote: str = "Надо позвонить Сергею",
    task_disposition: CandidateDisposition = CandidateDisposition.MATERIALIZE,
) -> ClassificationOutcome:
    return ClassificationOutcome(
        source_sha256="b" * 64,
        model_name=model_name,
        prompt_version="local-atomic-extraction-v1",
        schema_version="atomic-candidates-v1",
        candidates=(
            _candidate(
                CandidateType.TASK,
                task_quote,
                disposition=task_disposition,
                validation_code=CandidateValidationCode.VALID,
                modality=CandidateModality.COMMITMENT,
            ),
            _candidate(
                CandidateType.QUESTION,
                "Использовать Qdrant?",
                disposition=CandidateDisposition.NEEDS_REVIEW,
                validation_code=CandidateValidationCode.LOW_CONFIDENCE,
                modality=CandidateModality.QUESTION,
                confidence=0.8,
            ),
        ),
        discarded_candidate_count=1,
        skipped_reason=None,
    )


@pytest.mark.asyncio
async def test_completion_atomically_persists_result_task_and_provenance(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, capture_id, run_id, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=501
    )

    await _completion(engine).complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            outcome=_outcome(),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    async with schema_engine.connect() as connection:
        result = (
            await connection.execute(
                select(
                    ClassificationResultModel.processing_run_id,
                    ClassificationResultModel.capture_event_id,
                    ClassificationResultModel.candidates,
                    ClassificationResultModel.discarded_candidate_count,
                )
            )
        ).one()
        task = (
            await connection.execute(
                select(TaskModel.id, TaskModel.title, TaskModel.source_capture_event_id)
            )
        ).one()
        provenance = (
            await connection.execute(
                select(
                    TaskProvenanceModel.task_id,
                    TaskProvenanceModel.source_capture_event_id,
                )
            )
        ).one()
        step_status = await connection.scalar(
            select(ProcessingStepModel.status).where(ProcessingStepModel.id == step_id)
        )

    assert result.processing_run_id == run_id
    assert result.capture_event_id == capture_id
    assert result.discarded_candidate_count == 1
    assert [item["status"] for item in result.candidates] == [
        "materialized",
        "needs_review",
    ]
    assert result.candidates[0]["materialized_record_id"] == str(task.id)
    assert result.candidates[1]["materialized_record_id"] is None
    assert task.title == "Надо позвонить Сергею"
    assert task.source_capture_event_id == capture_id
    assert provenance == (task.id, capture_id)
    assert step_status == ProcessingStepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_auto_classified_task_with_time_sends_one_reminder_confirmation(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Баг тишины: кнопочный путь подтверждал «⏰ Напомню…», а авто-классификация
    # молчала — владелец не знал, что будильник заведён.
    _, _, _, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=504
    )
    spy = SpyConfirmationDelivery()

    await _completion(engine, spy).complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            outcome=_outcome(task_quote="Позвонить Сергею завтра в 10:00"),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    # NOW = 14.07 15:00 UTC = 18:00 Иерусалима → «завтра в 10:00» = 15.07 10:00
    # локального времени пространства; ровно ОДНО подтверждение получателю.
    assert spy.sent == [("⏰ Напомню 15.07.2026 10:00", 42)]
    async with schema_engine.connect() as connection:
        remind_at = await connection.scalar(select(ReminderModel.remind_at))
    assert remind_at == datetime(2026, 7, 15, 7, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_auto_classified_task_without_time_sends_no_confirmation(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, _, _, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=505
    )
    spy = SpyConfirmationDelivery()

    await _completion(engine, spy).complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            outcome=_outcome(),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    assert spy.sent == []
    async with schema_engine.connect() as connection:
        tasks = await connection.scalar(select(func.count()).select_from(TaskModel))
        reminders = await connection.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert tasks == 1  # задача создана — молчим только про напоминание
    assert reminders == 0


@pytest.mark.asyncio
async def test_non_materialized_candidate_with_time_sends_no_confirmation(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    # Форма кнопочного пути: задача уже создана капчей (pending-selection),
    # классификация видит её как already_captured → воркер НЕ подтверждает
    # второй раз (единственный ack остаётся за poller'ом).
    _, _, _, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=506
    )
    spy = SpyConfirmationDelivery()

    await _completion(engine, spy).complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            outcome=_outcome(
                task_quote="Позвонить Сергею завтра в 10:00",
                task_disposition=CandidateDisposition.ALREADY_CAPTURED,
            ),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    assert spy.sent == []
    async with schema_engine.connect() as connection:
        reminders = await connection.scalar(
            select(func.count()).select_from(ReminderModel)
        )
    assert reminders == 0


@pytest.mark.asyncio
async def test_confirmation_is_sent_only_after_commit_and_once_across_retry(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _, _, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=507
    )
    spy = SpyConfirmationDelivery()
    completion = _completion(engine, spy)

    with pytest.raises(DBAPIError):
        await completion.complete(
            CompleteClassificationCommand(
                access_context=ACCESS_A,
                step_id=step_id,
                outcome=_outcome(
                    model_name="x" * 256,
                    task_quote="Позвонить Сергею завтра в 10:00",
                ),
                completed_at=NOW + timedelta(seconds=2),
            )
        )

    # Транзакция откатилась — подтверждение НЕ уходило (шлём только после коммита).
    assert spy.sent == []

    failed = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            failed_at=NOW + timedelta(seconds=3),
            safe_error_code="classification_completion_failed",
        )
    )
    retry = await repository.claim_due_step(
        ACCESS_A,
        failed.next_attempt_at or NOW,
        LEASE,
        (ProcessingStepType.CLASSIFICATION,),
    )
    assert retry is not None
    await completion.complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=retry.step_id,
            outcome=_outcome(task_quote="Позвонить Сергею завтра в 10:00"),
            completed_at=NOW + timedelta(seconds=4),
        )
    )

    assert spy.sent == [("⏰ Напомню 15.07.2026 10:00", 42)]


@pytest.mark.asyncio
async def test_completion_rollback_then_retry_creates_one_result_and_task(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _, _, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=502
    )
    completion = _completion(engine)

    with pytest.raises(DBAPIError):
        await completion.complete(
            CompleteClassificationCommand(
                access_context=ACCESS_A,
                step_id=step_id,
                outcome=_outcome(model_name="x" * 256),
                completed_at=NOW + timedelta(seconds=2),
            )
        )

    async with schema_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count()).select_from(ClassificationResultModel)
            )
            == 0
        )
        assert await connection.scalar(select(func.count()).select_from(TaskModel)) == 0

    failed = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            failed_at=NOW + timedelta(seconds=3),
            safe_error_code="classification_completion_failed",
        )
    )
    retry = await repository.claim_due_step(
        ACCESS_A,
        failed.next_attempt_at or NOW,
        LEASE,
        (ProcessingStepType.CLASSIFICATION,),
    )
    assert retry is not None
    await completion.complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=retry.step_id,
            outcome=_outcome(),
            completed_at=NOW + timedelta(minutes=2),
        )
    )

    async with schema_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count()).select_from(ClassificationResultModel)
            )
            == 1
        )
        assert await connection.scalar(select(func.count()).select_from(TaskModel)) == 1


@pytest.mark.asyncio
async def test_other_space_cannot_complete_or_observe_result(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, _, run_id, step_id = await _claimed_text_run(
        engine, schema_engine, ACCESS_A, update_id=503
    )
    completion = _completion(engine)

    with pytest.raises(LookupError):
        await completion.complete(
            CompleteClassificationCommand(
                access_context=ACCESS_B,
                step_id=step_id,
                outcome=_outcome(),
                completed_at=NOW,
            )
        )

    await completion.complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=step_id,
            outcome=_outcome(),
            completed_at=NOW,
        )
    )
    repository = PostgresClassificationRepository(create_session_factory(engine))
    assert await repository.count_results(ACCESS_A) == 1
    assert await repository.count_results(ACCESS_B) == 0
    assert await repository.get_result(ACCESS_A, run_id) is not None
    assert await repository.get_result(ACCESS_B, run_id) is None


@pytest.mark.asyncio
async def test_classification_table_has_forced_rls_and_minimal_privileges(
    engine: AsyncEngine, isolated_database: IsolatedDatabase
) -> None:
    qualified = f'"{isolated_database.schema}"."classification_results"'
    async with create_session_factory(engine)() as session:
        flags = (
            await session.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c WHERE c.oid = to_regclass(:table_name)"
                ),
                {"table_name": qualified},
            )
        ).one()
        assert flags == (True, True)
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            granted = await session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table_name, :privilege)"
                ),
                {"table_name": qualified, "privilege": privilege},
            )
            assert granted is (privilege in {"SELECT", "INSERT"})

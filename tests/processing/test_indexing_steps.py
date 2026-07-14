from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import User, UserSpace
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingStepModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CreateTextProcessingRunCommand,
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingRun,
    ProcessingStep,
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
INDEXING_STEPS = (ProcessingStepType.INDEXING,)
CLASSIFICATION_STEPS = (ProcessingStepType.CLASSIFICATION,)
ACCESS_A = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000012"),
)


@pytest_asyncio.fixture(autouse=True)
async def reset_processing_schema(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(insert(User), [_user(ACCESS_A), _user(ACCESS_B)])
        await connection.execute(
            insert(UserSpace), [_space(ACCESS_A), _space(ACCESS_B)]
        )


def _user(access: AccessContext) -> dict[str, object]:
    return {
        "id": access.user_id,
        "role": "admin",
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


async def _add_capture(
    schema_engine: AsyncEngine, access: AccessContext, *, update_id: int
) -> UUID:
    capture_event_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_event_id,
                user_space_id=access.user_space_id,
                channel="telegram",
                bot_id=100,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 1_000,
                raw_text="temporary parent",
                received_at=NOW,
                created_at=NOW,
                trace_id=f"{update_id:x}".rjust(32, "1")[-32:],
            )
        )
    return capture_event_id


async def _create_text_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
) -> tuple[PostgresProcessingRepository, ProcessingRun]:
    capture_event_id = await _add_capture(schema_engine, access, update_id=update_id)
    repository = PostgresProcessingRepository(create_session_factory(engine))
    run = await repository.create_text_run(
        CreateTextProcessingRunCommand(
            access_context=access,
            capture_event_id=capture_event_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=f"{update_id:x}".rjust(32, "2")[-32:],
        )
    )
    return repository, run


async def _create_voice_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
) -> tuple[PostgresProcessingRepository, ProcessingRun]:
    capture_event_id = await _add_capture(schema_engine, access, update_id=update_id)
    repository = PostgresProcessingRepository(create_session_factory(engine))
    run = await repository.create_voice_run(
        CreateVoiceProcessingRunCommand(
            access_context=access,
            capture_event_id=capture_event_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=f"{update_id:x}".rjust(32, "3")[-32:],
        )
    )
    return repository, run


async def _succeed_next(
    repository: PostgresProcessingRepository,
    access: AccessContext,
    step_types: tuple[ProcessingStepType, ...],
    at: datetime,
) -> None:
    claim = await repository.claim_due_step(access, at, LEASE, step_types)
    assert claim is not None
    await repository.succeed_step(
        SucceedProcessingStepCommand(
            access_context=access, step_id=claim.step_id, completed_at=at
        )
    )


async def _fail_to_final(
    repository: PostgresProcessingRepository,
    access: AccessContext,
    step_types: tuple[ProcessingStepType, ...],
    *,
    start: datetime,
) -> tuple[ProcessingStep, datetime]:
    at = start
    step: ProcessingStep | None = None
    for _ in range(3):
        claim = await repository.claim_due_step(access, at, LEASE, step_types)
        assert claim is not None
        at = at + timedelta(seconds=10)
        step = await repository.fail_step(
            FailProcessingStepCommand(
                access_context=access,
                step_id=claim.step_id,
                failed_at=at,
                safe_error_code="step_failed",
            )
        )
        at = step.next_attempt_at or at
    assert step is not None
    return step, at


def _statuses(run: ProcessingRun) -> dict[ProcessingStepType, ProcessingStepStatus]:
    return {step.step_type: step.status for step in run.steps}


@pytest.mark.asyncio
async def test_text_run_creates_pending_classification_and_indexing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, run = await _create_text_run(engine, schema_engine, ACCESS_A, update_id=401)

    assert [step.step_type for step in run.steps] == [
        ProcessingStepType.CLASSIFICATION,
        ProcessingStepType.INDEXING,
    ]
    assert all(step.status is ProcessingStepStatus.PENDING for step in run.steps)
    assert all(step.attempt_count == 0 for step in run.steps)


@pytest.mark.asyncio
async def test_voice_run_creates_four_ordered_pending_steps(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_voice_run(
        engine, schema_engine, ACCESS_A, update_id=402
    )

    expected_order = [
        ProcessingStepType.AUDIO_DOWNLOAD,
        ProcessingStepType.TRANSCRIPTION,
        ProcessingStepType.CLASSIFICATION,
        ProcessingStepType.INDEXING,
    ]
    assert [step.step_type for step in run.steps] == expected_order
    assert all(step.status is ProcessingStepStatus.PENDING for step in run.steps)
    assert all(step.attempt_count == 0 for step in run.steps)

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    assert [step.step_type for step in loaded.steps] == expected_order


@pytest.mark.asyncio
async def test_text_indexing_is_claimable_before_classification(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=403
    )

    claim = await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS)
    assert claim is not None
    assert claim.step_type is ProcessingStepType.INDEXING
    assert claim.run_id == run.id
    assert claim.capture_event_id == run.capture_event_id
    assert claim.attempt_count == 1
    assert claim.lease_expires_at == NOW + LEASE

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    assert _statuses(loaded)[ProcessingStepType.CLASSIFICATION] is (
        ProcessingStepStatus.PENDING
    )


@pytest.mark.asyncio
async def test_voice_indexing_waits_only_for_transcription(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_voice_run(
        engine, schema_engine, ACCESS_A, update_id=404
    )

    assert await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS) is None
    await _succeed_next(repository, ACCESS_A, (ProcessingStepType.AUDIO_DOWNLOAD,), NOW)
    assert await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS) is None
    await _succeed_next(repository, ACCESS_A, (ProcessingStepType.TRANSCRIPTION,), NOW)

    claim = await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS)
    assert claim is not None
    assert claim.step_type is ProcessingStepType.INDEXING

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    assert _statuses(loaded)[ProcessingStepType.CLASSIFICATION] is (
        ProcessingStepStatus.PENDING
    )


@pytest.mark.asyncio
async def test_final_transcription_failure_skips_indexing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_voice_run(
        engine, schema_engine, ACCESS_A, update_id=405
    )
    await _succeed_next(repository, ACCESS_A, (ProcessingStepType.AUDIO_DOWNLOAD,), NOW)

    final, _ = await _fail_to_final(
        repository, ACCESS_A, (ProcessingStepType.TRANSCRIPTION,), start=NOW
    )
    assert final.status is ProcessingStepStatus.FAILED

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    statuses = _statuses(loaded)
    assert statuses[ProcessingStepType.CLASSIFICATION] is ProcessingStepStatus.SKIPPED
    assert statuses[ProcessingStepType.INDEXING] is ProcessingStepStatus.SKIPPED


@pytest.mark.asyncio
async def test_final_download_failure_skips_indexing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_voice_run(
        engine, schema_engine, ACCESS_A, update_id=406
    )

    final, _ = await _fail_to_final(
        repository, ACCESS_A, (ProcessingStepType.AUDIO_DOWNLOAD,), start=NOW
    )
    assert final.status is ProcessingStepStatus.FAILED

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    statuses = _statuses(loaded)
    assert statuses[ProcessingStepType.TRANSCRIPTION] is ProcessingStepStatus.SKIPPED
    assert statuses[ProcessingStepType.CLASSIFICATION] is ProcessingStepStatus.SKIPPED
    assert statuses[ProcessingStepType.INDEXING] is ProcessingStepStatus.SKIPPED


@pytest.mark.asyncio
async def test_final_classification_failure_leaves_indexing_untouched(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=407
    )

    final, after = await _fail_to_final(
        repository, ACCESS_A, CLASSIFICATION_STEPS, start=NOW
    )
    assert final.status is ProcessingStepStatus.FAILED

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    assert _statuses(loaded)[ProcessingStepType.INDEXING] is (
        ProcessingStepStatus.PENDING
    )
    claim = await repository.claim_due_step(ACCESS_A, after, LEASE, INDEXING_STEPS)
    assert claim is not None
    assert claim.step_type is ProcessingStepType.INDEXING

    notice = await repository.claim_due_notice(ACCESS_A, after)
    assert notice is not None
    assert notice.kind.value == "failure"
    assert notice.run_id == run.id


@pytest.mark.asyncio
async def test_indexing_retries_with_backoff_like_every_other_step(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _ = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=408
    )

    claim_1 = await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS)
    assert claim_1 is not None
    failure_1_at = NOW + timedelta(seconds=10)
    retry_1 = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=claim_1.step_id,
            failed_at=failure_1_at,
            safe_error_code="embedding_failed",
        )
    )
    assert retry_1.status is ProcessingStepStatus.PENDING
    assert retry_1.next_attempt_at == failure_1_at + timedelta(minutes=1)
    assert (
        await repository.claim_due_step(
            ACCESS_A,
            retry_1.next_attempt_at - timedelta(microseconds=1),
            LEASE,
            INDEXING_STEPS,
        )
        is None
    )

    claim_2 = await repository.claim_due_step(
        ACCESS_A, retry_1.next_attempt_at, LEASE, INDEXING_STEPS
    )
    assert claim_2 is not None
    assert claim_2.attempt_count == 2
    failure_2_at = retry_1.next_attempt_at + timedelta(seconds=10)
    retry_2 = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=claim_2.step_id,
            failed_at=failure_2_at,
            safe_error_code="embedding_failed",
        )
    )
    assert retry_2.status is ProcessingStepStatus.PENDING
    assert retry_2.next_attempt_at == failure_2_at + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_database_rejects_indexing_step_with_unknown_status(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, run = await _create_text_run(engine, schema_engine, ACCESS_A, update_id=409)

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(ProcessingStepModel).values(
                    id=uuid4(),
                    user_space_id=ACCESS_A.user_space_id,
                    processing_run_id=run.id,
                    step_type="indexing",
                    status=99,
                    attempt_count=0,
                    next_attempt_at=NOW,
                    lease_expires_at=None,
                    safe_error_code=None,
                    started_at=None,
                    completed_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                    trace_id="a" * 32,
                )
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_lock_indexing_target_requires_a_running_indexing_step(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=410
    )

    pending_indexing = next(
        step for step in run.steps if step.step_type is ProcessingStepType.INDEXING
    )
    with pytest.raises(ValueError, match="running"):
        await repository.lock_indexing_target(ACCESS_A, pending_indexing.id)

    classification = await repository.claim_due_step(
        ACCESS_A, NOW, LEASE, CLASSIFICATION_STEPS
    )
    assert classification is not None
    with pytest.raises(ValueError, match="not an indexing"):
        await repository.lock_indexing_target(ACCESS_A, classification.step_id)

    indexing = await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS)
    assert indexing is not None
    target = await repository.lock_indexing_target(ACCESS_A, indexing.step_id)
    assert target.step_id == indexing.step_id
    assert target.run_id == run.id
    assert target.capture_event_id == run.capture_event_id
    assert target.output_type is TranscriptionOutputType.NOTE
    assert target.version == run.version
    assert target.trace_id == run.trace_id


@pytest.mark.asyncio
async def test_claim_indexing_never_crosses_access_context(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run_b = await _create_text_run(
        engine, schema_engine, ACCESS_B, update_id=411
    )

    assert await repository.claim_due_step(ACCESS_A, NOW, LEASE, INDEXING_STEPS) is None
    claim = await repository.claim_due_step(ACCESS_B, NOW, LEASE, INDEXING_STEPS)
    assert claim is not None
    assert claim.run_id == run_b.id


@pytest.mark.asyncio
async def test_final_indexing_failure_creates_no_notice(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=412
    )

    final, after = await _fail_to_final(repository, ACCESS_A, INDEXING_STEPS, start=NOW)
    assert final.status is ProcessingStepStatus.FAILED
    assert final.next_attempt_at is None
    assert final.safe_error_code == "step_failed"

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    assert _statuses(loaded)[ProcessingStepType.CLASSIFICATION] is (
        ProcessingStepStatus.PENDING
    )
    assert (
        await repository.claim_due_notice(ACCESS_A, after + timedelta(days=1)) is None
    )


@pytest.mark.asyncio
async def test_exhausted_indexing_lease_finalizes_without_notice(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=413
    )

    at = NOW
    for _ in range(2):
        claim = await repository.claim_due_step(ACCESS_A, at, LEASE, INDEXING_STEPS)
        assert claim is not None
        failed_at = at + timedelta(seconds=10)
        step = await repository.fail_step(
            FailProcessingStepCommand(
                access_context=ACCESS_A,
                step_id=claim.step_id,
                failed_at=failed_at,
                safe_error_code="embedding_failed",
            )
        )
        assert step.next_attempt_at is not None
        at = step.next_attempt_at
    last_claim = await repository.claim_due_step(ACCESS_A, at, LEASE, INDEXING_STEPS)
    assert last_claim is not None
    assert last_claim.attempt_count == 3

    after_lease = at + LEASE
    assert (
        await repository.claim_due_step(ACCESS_A, after_lease, LEASE, INDEXING_STEPS)
        is None
    )
    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    indexing = next(
        step for step in loaded.steps if step.step_type is ProcessingStepType.INDEXING
    )
    assert indexing.status is ProcessingStepStatus.FAILED
    assert indexing.safe_error_code == "lease_expired"
    assert (
        await repository.claim_due_notice(ACCESS_A, after_lease + timedelta(days=1))
        is None
    )


@pytest.mark.asyncio
async def test_exhausted_transcription_lease_still_notifies_and_skips_indexing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run = await _create_voice_run(
        engine, schema_engine, ACCESS_A, update_id=414
    )
    await _succeed_next(repository, ACCESS_A, (ProcessingStepType.AUDIO_DOWNLOAD,), NOW)

    at = NOW
    for _ in range(2):
        claim = await repository.claim_due_step(
            ACCESS_A, at, LEASE, (ProcessingStepType.TRANSCRIPTION,)
        )
        assert claim is not None
        failed_at = at + timedelta(seconds=10)
        step = await repository.fail_step(
            FailProcessingStepCommand(
                access_context=ACCESS_A,
                step_id=claim.step_id,
                failed_at=failed_at,
                safe_error_code="transcription_failed",
            )
        )
        assert step.next_attempt_at is not None
        at = step.next_attempt_at
    last_claim = await repository.claim_due_step(
        ACCESS_A, at, LEASE, (ProcessingStepType.TRANSCRIPTION,)
    )
    assert last_claim is not None
    assert last_claim.attempt_count == 3

    after_lease = at + LEASE
    assert (
        await repository.claim_due_step(
            ACCESS_A, after_lease, LEASE, (ProcessingStepType.TRANSCRIPTION,)
        )
        is None
    )
    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    statuses = _statuses(loaded)
    assert statuses[ProcessingStepType.TRANSCRIPTION] is ProcessingStepStatus.FAILED
    assert statuses[ProcessingStepType.CLASSIFICATION] is ProcessingStepStatus.SKIPPED
    assert statuses[ProcessingStepType.INDEXING] is ProcessingStepStatus.SKIPPED
    notice = await repository.claim_due_notice(
        ACCESS_A, after_lease + timedelta(days=1)
    )
    assert notice is not None
    assert notice.kind.value == "failure"
    assert notice.run_id == run.id


@pytest.mark.asyncio
async def test_claim_orders_classification_before_indexing(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, _ = await _create_text_run(
        engine, schema_engine, ACCESS_A, update_id=415
    )
    both = (ProcessingStepType.CLASSIFICATION, ProcessingStepType.INDEXING)

    first = await repository.claim_due_step(ACCESS_A, NOW, LEASE, both)
    assert first is not None
    assert first.step_type is ProcessingStepType.CLASSIFICATION

    second = await repository.claim_due_step(ACCESS_A, NOW, LEASE, both)
    assert second is not None
    assert second.step_type is ProcessingStepType.INDEXING

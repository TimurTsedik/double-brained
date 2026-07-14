from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
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
    ProcessingNoticeModel,
    ProcessingRunModel,
    ProcessingStepModel,
    TranscriptModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    MarkProcessingNoticeSentCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
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


async def _create_run(
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
    access: AccessContext,
    *,
    update_id: int,
):
    capture_event_id = await _add_capture(schema_engine, access, update_id=update_id)
    repository = PostgresProcessingRepository(create_session_factory(engine))
    run = await repository.create_voice_run(
        CreateVoiceProcessingRunCommand(
            access_context=access,
            capture_event_id=capture_event_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=f"{update_id:x}".rjust(32, "2")[-32:],
        )
    )
    return repository, run, capture_event_id


@pytest.mark.asyncio
async def test_voice_run_starts_with_two_ordered_pending_steps(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, run, _ = await _create_run(engine, schema_engine, ACCESS_A, update_id=101)

    assert run.user_space_id == ACCESS_A.user_space_id
    assert run.output_type is TranscriptionOutputType.NOTE
    assert [step.step_type for step in run.steps] == [
        ProcessingStepType.AUDIO_DOWNLOAD,
        ProcessingStepType.TRANSCRIPTION,
    ]
    assert [step.status for step in run.steps] == [
        ProcessingStepStatus.PENDING,
        ProcessingStepStatus.PENDING,
    ]
    assert run.overall_status is ProcessingStepStatus.PENDING


@pytest.mark.asyncio
async def test_claim_obeys_dependency_and_reclaims_only_an_expired_lease(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run, _ = await _create_run(
        engine, schema_engine, ACCESS_A, update_id=102
    )

    first = await repository.claim_due_step(ACCESS_A, NOW, LEASE)
    assert first is not None
    assert first.step_type is ProcessingStepType.AUDIO_DOWNLOAD
    assert first.attempt_count == 1
    assert first.lease_expires_at == NOW + LEASE

    assert await repository.claim_due_step(ACCESS_A, NOW + LEASE / 2, LEASE) is None

    reclaimed = await repository.claim_due_step(ACCESS_A, NOW + LEASE, LEASE)
    assert reclaimed is not None
    assert reclaimed.step_id == first.step_id
    assert reclaimed.attempt_count == 2

    await repository.succeed_step(
        SucceedProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=reclaimed.step_id,
            completed_at=NOW + LEASE,
        )
    )
    transcription = await repository.claim_due_step(ACCESS_A, NOW + LEASE, LEASE)
    assert transcription is not None
    assert transcription.run_id == run.id
    assert transcription.step_type is ProcessingStepType.TRANSCRIPTION


@pytest.mark.asyncio
async def test_retry_backoff_and_final_download_failure_skip_transcription(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository, run, _ = await _create_run(
        engine, schema_engine, ACCESS_A, update_id=103
    )

    claim_1 = await repository.claim_due_step(ACCESS_A, NOW, LEASE)
    assert claim_1 is not None
    failure_1_at = NOW + timedelta(seconds=10)
    retry_1 = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=claim_1.step_id,
            failed_at=failure_1_at,
            safe_error_code="telegram_unavailable",
        )
    )
    assert retry_1.status is ProcessingStepStatus.PENDING
    assert await repository.claim_due_notice(ACCESS_A, failure_1_at) is None
    assert retry_1.next_attempt_at == failure_1_at + timedelta(minutes=1)
    assert (
        await repository.claim_due_step(
            ACCESS_A, retry_1.next_attempt_at - timedelta(microseconds=1), LEASE
        )
        is None
    )

    claim_2 = await repository.claim_due_step(ACCESS_A, retry_1.next_attempt_at, LEASE)
    assert claim_2 is not None
    failure_2_at = retry_1.next_attempt_at + timedelta(seconds=10)
    retry_2 = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=claim_2.step_id,
            failed_at=failure_2_at,
            safe_error_code="telegram_unavailable",
        )
    )
    assert retry_2.status is ProcessingStepStatus.PENDING
    assert await repository.claim_due_notice(ACCESS_A, failure_2_at) is None
    assert retry_2.next_attempt_at == failure_2_at + timedelta(minutes=5)

    claim_3 = await repository.claim_due_step(ACCESS_A, retry_2.next_attempt_at, LEASE)
    assert claim_3 is not None
    final = await repository.fail_step(
        FailProcessingStepCommand(
            access_context=ACCESS_A,
            step_id=claim_3.step_id,
            failed_at=retry_2.next_attempt_at + timedelta(seconds=10),
            safe_error_code="telegram_unavailable",
        )
    )
    assert final.status is ProcessingStepStatus.FAILED
    assert final.next_attempt_at is None

    loaded = await repository.get_run(ACCESS_A, run.id)
    assert loaded is not None
    assert [step.status for step in loaded.steps] == [
        ProcessingStepStatus.FAILED,
        ProcessingStepStatus.SKIPPED,
    ]
    assert loaded.overall_status is ProcessingStepStatus.FAILED

    notice = await repository.claim_due_notice(
        ACCESS_A, retry_2.next_attempt_at + timedelta(seconds=10)
    )
    assert notice is not None
    assert notice.kind.value == "failure"
    assert notice.run_id == run.id
    assert notice.trace_id == run.trace_id
    assert await repository.claim_due_notice(ACCESS_B, NOW + timedelta(days=1)) is None
    await repository.mark_notice_sent(
        MarkProcessingNoticeSentCommand(
            access_context=ACCESS_A,
            notice_id=notice.notice_id,
            sent_at=NOW + timedelta(days=1),
        )
    )
    assert await repository.claim_due_notice(ACCESS_A, NOW + timedelta(days=2)) is None


@pytest.mark.asyncio
async def test_rls_hides_and_prevents_claiming_another_space(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository_a, run_a, _ = await _create_run(
        engine, schema_engine, ACCESS_A, update_id=104
    )
    _, run_b, _ = await _create_run(engine, schema_engine, ACCESS_B, update_id=105)

    assert await repository_a.get_run(ACCESS_A, run_a.id) is not None
    assert await repository_a.get_run(ACCESS_A, run_b.id) is None
    assert await repository_a.count_runs(ACCESS_A) == 1

    claimed_a = await repository_a.claim_due_step(ACCESS_A, NOW, LEASE)
    assert claimed_a is not None
    assert claimed_a.run_id == run_a.id
    assert await repository_a.claim_due_step(ACCESS_A, NOW, LEASE) is None


@pytest.mark.asyncio
async def test_processing_tables_have_forced_rls_and_worker_privileges(
    engine: AsyncEngine, isolated_database: IsolatedDatabase
) -> None:
    expected_privileges = {
        "processing_runs": {"SELECT", "INSERT"},
        "processing_steps": {"SELECT", "INSERT", "UPDATE"},
        "transcripts": {"SELECT", "INSERT"},
        "processing_notices": {"SELECT", "INSERT", "UPDATE"},
    }
    async with create_session_factory(engine)() as session:
        for table_name, granted in expected_privileges.items():
            qualified_table = f'"{isolated_database.schema}"."{table_name}"'
            flags = (
                await session.execute(
                    text(
                        "SELECT c.relrowsecurity, c.relforcerowsecurity "
                        "FROM pg_class c "
                        "WHERE c.oid = to_regclass(:table_name)"
                    ),
                    {"table_name": qualified_table},
                )
            ).one()
            assert flags == (True, True)

            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                has_privilege = await session.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :table_name, "
                        ":privilege)"
                    ),
                    {"table_name": qualified_table, "privilege": privilege},
                )
                assert has_privilege is (privilege in granted)


@pytest.mark.asyncio
async def test_database_rejects_unknown_status_and_cross_space_transcript(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    repository_a, run_a, capture_a = await _create_run(
        engine, schema_engine, ACCESS_A, update_id=106
    )
    _, run_b, capture_b = await _create_run(
        engine, schema_engine, ACCESS_B, update_id=107
    )

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(ProcessingStepModel).values(
                    id=uuid4(),
                    user_space_id=ACCESS_A.user_space_id,
                    processing_run_id=run_a.id,
                    step_type="audio_download",
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

    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(TranscriptModel).values(
                    id=uuid4(),
                    user_space_id=ACCESS_A.user_space_id,
                    capture_event_id=capture_b,
                    processing_run_id=run_b.id,
                    version=1,
                    text="must not cross spaces",
                    language="ru",
                    language_probability=0.9,
                    model_name="local-model",
                    segments=[],
                    created_at=NOW,
                    trace_id="b" * 32,
                )
            )
        await transaction.rollback()

    assert await repository_a.get_run(ACCESS_A, run_a.id) is not None
    assert capture_a != capture_b


@pytest.mark.asyncio
async def test_transcript_version_is_unique_per_source_and_space(
    schema_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    _, run, capture_event_id = await _create_run(
        engine, schema_engine, ACCESS_A, update_id=108
    )
    values = {
        "user_space_id": ACCESS_A.user_space_id,
        "capture_event_id": capture_event_id,
        "processing_run_id": run.id,
        "version": 1,
        "text": "private transcript",
        "language": "ru",
        "language_probability": None,
        "model_name": "local-model",
        "segments": [],
        "created_at": NOW,
        "trace_id": "c" * 32,
    }
    async with schema_engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(insert(TranscriptModel).values(id=uuid4(), **values))
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(TranscriptModel).values(id=uuid4(), **values)
            )
        await transaction.rollback()


@pytest.mark.asyncio
async def test_rls_applies_to_all_processing_content_tables(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    _, run_b, capture_b = await _create_run(
        engine, schema_engine, ACCESS_B, update_id=109
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(TranscriptModel).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                capture_event_id=capture_b,
                processing_run_id=run_b.id,
                version=1,
                text="B-only transcript",
                language="ru",
                language_probability=0.9,
                model_name="local-model",
                segments=[],
                created_at=NOW,
                trace_id="d" * 32,
            )
        )
        await connection.execute(
            insert(ProcessingNoticeModel).values(
                id=uuid4(),
                user_space_id=ACCESS_B.user_space_id,
                processing_run_id=run_b.id,
                kind="success",
                status="pending",
                attempt_count=0,
                next_attempt_at=NOW,
                sent_at=None,
                created_at=NOW,
                updated_at=NOW,
                trace_id="e" * 32,
            )
        )

    async with create_session_factory(engine)() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('second_brain.user_space_id', "
                    ":user_space_id, true)"
                ),
                {"user_space_id": str(ACCESS_A.user_space_id)},
            )
            for model in (
                ProcessingRunModel,
                ProcessingStepModel,
                TranscriptModel,
                ProcessingNoticeModel,
            ):
                count = await session.scalar(select(func.count()).select_from(model))
                assert count == 0

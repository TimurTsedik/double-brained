"""Интеграция image-прогона: зависимости шагов, completion, уведомления.

Ключевые контракты спеки §2: classification/indexing НЕ гейтятся download'ом
(текст подписи независим от байтов картинки); завершение download'а атомарно
пишет storage-метаданные на attachment; source-only прогон (без подписи)
успешного notice НЕ создаёт; финальный провал download'а даёт failure-notice,
НЕ трогая classification/indexing.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.image_processing_completion import (
    ImageDownloadCompletionInTransaction,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    User,
    UserSpace,
)
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingNoticeModel,
    ProcessingStepModel,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
    CreateImageProcessingRunCommand,
    FailProcessingStepCommand,
    StoredImage,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingNoticeKind,
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from tests.identity.conftest import IsolatedDatabase

NOW = datetime(2026, 7, 18, 14, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
TRACE_ID = "8" * 32
LEASE = timedelta(minutes=15)


@pytest_asyncio.fixture(autouse=True)
async def image_worker_database(
    isolated_database: IsolatedDatabase, schema_engine: AsyncEngine
) -> None:
    await reset_prototype_schema(
        schema_engine, confirm=True, schema_name=isolated_database.schema
    )
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ACCESS.user_id,
                role="member",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(UserSpace).values(
                id=ACCESS.user_space_id,
                owner_user_id=ACCESS.user_id,
                timezone="Asia/Jerusalem",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(TelegramIdentity).values(
                id=uuid4(),
                telegram_user_id=555,
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


async def _seed_image_capture(
    schema_engine: AsyncEngine, *, caption: str | None
) -> UUID:
    capture_id = uuid4()
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(CaptureEventModel).values(
                id=capture_id,
                user_space_id=ACCESS.user_space_id,
                source_kind="image",
                channel="telegram",
                bot_id=10,
                telegram_update_id=20,
                telegram_message_id=30,
                raw_text=caption,
                received_at=NOW,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
        await connection.execute(
            insert(TelegramAttachmentModel).values(
                id=uuid4(),
                user_space_id=ACCESS.user_space_id,
                capture_event_id=capture_id,
                kind="image",
                telegram_file_id="private-photo-id",
                telegram_file_unique_id="private-photo-unique",
                duration_seconds=None,
                width=1280,
                height=853,
                telegram_file_size=222_333,
                telegram_mime_type=None,
                storage_key=None,
                sha256=None,
                stored_size=None,
                stored_mime_type=None,
                stored_at=None,
                created_at=NOW,
                trace_id=TRACE_ID,
            )
        )
    return capture_id


def _stored_image(capture_id: UUID) -> StoredImage:
    return StoredImage(
        storage_key=f"{ACCESS.user_space_id}/{capture_id}/original.jpg",
        local_path="/private/original.jpg",
        sha256="b" * 64,
        size=17,
        mime_type="image/jpeg",
    )


@pytest.mark.asyncio
async def test_caption_run_classification_and_indexing_do_not_wait_for_download(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_id = await _seed_image_capture(schema_engine, caption="подпись")
    repository = PostgresProcessingRepository(create_session_factory(engine))
    await repository.create_image_run(
        CreateImageProcessingRunCommand(
            access_context=ACCESS,
            capture_event_id=capture_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=TRACE_ID,
        )
    )

    # Download ещё даже не стартовал — classification уже claimable.
    classification = await repository.claim_due_step(
        ACCESS, NOW + timedelta(seconds=1), LEASE, (ProcessingStepType.CLASSIFICATION,)
    )
    indexing = await repository.claim_due_step(
        ACCESS, NOW + timedelta(seconds=2), LEASE, (ProcessingStepType.INDEXING,)
    )

    assert classification is not None
    assert classification.step_type is ProcessingStepType.CLASSIFICATION
    assert classification.output_type is TranscriptionOutputType.NOTE
    assert indexing is not None
    assert indexing.step_type is ProcessingStepType.INDEXING


@pytest.mark.asyncio
async def test_download_completion_marks_attachment_stored_and_step_succeeded(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_id = await _seed_image_capture(schema_engine, caption=None)
    session_factory = create_session_factory(engine)
    repository = PostgresProcessingRepository(session_factory)
    await repository.create_image_run(
        CreateImageProcessingRunCommand(
            access_context=ACCESS,
            capture_event_id=capture_id,
            output_type=None,
            created_at=NOW,
            trace_id=TRACE_ID,
        )
    )
    download = await repository.claim_due_step(
        ACCESS, NOW + timedelta(seconds=1), LEASE, (ProcessingStepType.IMAGE_DOWNLOAD,)
    )
    assert download is not None
    assert download.output_type is None

    await ImageDownloadCompletionInTransaction(session_factory).complete(
        CompleteImageDownloadCommand(
            access_context=ACCESS,
            step_id=download.step_id,
            capture_event_id=capture_id,
            stored_image=_stored_image(capture_id),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    async with schema_engine.connect() as connection:
        attachment = (
            await connection.execute(
                select(
                    TelegramAttachmentModel.storage_key,
                    TelegramAttachmentModel.sha256,
                    TelegramAttachmentModel.stored_size,
                    TelegramAttachmentModel.stored_mime_type,
                )
            )
        ).one()
        step_status = await connection.scalar(
            select(ProcessingStepModel.status).where(
                ProcessingStepModel.id == download.step_id
            )
        )
        notices = await connection.scalar(
            select(func.count()).select_from(ProcessingNoticeModel)
        )
    assert attachment.storage_key == (
        f"{ACCESS.user_space_id}/{capture_id}/original.jpg"
    )
    assert attachment.sha256 == "b" * 64
    assert attachment.stored_size == 17
    assert attachment.stored_mime_type == "image/jpeg"
    assert step_status == ProcessingStepStatus.SUCCEEDED.value
    # Успех source-only прогона НЕ объявляется: success-notice не создаётся.
    assert notices == 0


@pytest.mark.asyncio
async def test_final_download_failure_notices_but_keeps_caption_steps_alive(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    capture_id = await _seed_image_capture(schema_engine, caption="подпись")
    repository = PostgresProcessingRepository(create_session_factory(engine))
    await repository.create_image_run(
        CreateImageProcessingRunCommand(
            access_context=ACCESS,
            capture_event_id=capture_id,
            output_type=TranscriptionOutputType.NOTE,
            created_at=NOW,
            trace_id=TRACE_ID,
        )
    )

    # Исчерпываем все попытки download'а (разные моменты — вечный урок).
    moment = NOW
    for attempt in range(3):
        moment = moment + timedelta(minutes=10 * attempt + 1)
        download = await repository.claim_due_step(
            ACCESS, moment, LEASE, (ProcessingStepType.IMAGE_DOWNLOAD,)
        )
        assert download is not None
        await repository.fail_step(
            FailProcessingStepCommand(
                access_context=ACCESS,
                step_id=download.step_id,
                failed_at=moment + timedelta(seconds=1),
                safe_error_code="telegram_download_failed",
            )
        )

    async with schema_engine.connect() as connection:
        steps = (
            await connection.execute(
                select(ProcessingStepModel.step_type, ProcessingStepModel.status)
            )
        ).all()
        notice_kind = await connection.scalar(select(ProcessingNoticeModel.kind))
    statuses = {step.step_type: step.status for step in steps}
    assert statuses[ProcessingStepType.IMAGE_DOWNLOAD] == (
        ProcessingStepStatus.FAILED.value
    )
    # Подпись независима от байтов: classification/indexing НЕ скипаются.
    assert statuses[ProcessingStepType.CLASSIFICATION] == (
        ProcessingStepStatus.PENDING.value
    )
    assert statuses[ProcessingStepType.INDEXING] == ProcessingStepStatus.PENDING.value
    assert notice_kind is ProcessingNoticeKind.FAILURE

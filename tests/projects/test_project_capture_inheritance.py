from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.classification_completion import (
    ClassificationCompletionInTransaction,
)
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.bootstrap.voice_capture_in_transaction import (
    VoiceCaptureInTransaction,
)
from second_brain.bootstrap.voice_processing_completion import (
    VoiceDownloadCompletionInTransaction,
    VoiceTranscriptionCompletionInTransaction,
)
from second_brain.shared.i18n import Locale
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    CaptureVoiceCommand,
    TelegramVoiceMetadata,
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
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
)
from second_brain.slices.processing.adapters.persistence.repository import (
    PostgresProcessingRepository,
)
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    StoredVoice,
    TranscriptionDraft,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepType,
    TranscriptSegment,
    TranscriptWord,
)
from second_brain.slices.projects.adapters.persistence.models import (
    ProjectCaptureEventLinkModel,
    ProjectDecisionLinkModel,
    ProjectIdeaLinkModel,
    ProjectNoteLinkModel,
    ProjectQuestionLinkModel,
    ProjectTaskLinkModel,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkRepository,
    PostgresProjectRepository,
)
from second_brain.slices.projects.application.contracts import (
    LinkProjectContentCommand,
)
from second_brain.slices.projects.application.projects import Projects
from second_brain.slices.projects.domain.entities import ProjectContentKind
from second_brain.slices.tasks.application.contracts import (
    SetPendingCaptureSelectionCommand,
)
from tests.projects.conftest import ACCESS_A, NOW
from tests.projects.test_project_persistence import (
    TRACE_ID,
    create_project,
)


class NullConfirmationDelivery:
    async def deliver(self, text: str, recipient: TelegramRecipient) -> None:
        return None


class FixedWorkerIdentity:
    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        return (ACCESS_A,)

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        return TelegramRecipient(telegram_user_id=42)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        return Locale.RU


LINK_MODELS = {
    "note": ProjectNoteLinkModel,
    "task": ProjectTaskLinkModel,
    "idea": ProjectIdeaLinkModel,
    "decision": ProjectDecisionLinkModel,
    "question": ProjectQuestionLinkModel,
}


async def capture_text(
    engine: AsyncEngine, update_id: int, selection: str = "note"
) -> UUID:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        transaction = PostgresUpdateTransaction(session)
        capture = TaskCaptureInTransaction()
        if selection != "note":
            await capture.set_selection(
                SetPendingCaptureSelectionCommand(ACCESS_A, selection, NOW, TRACE_ID),
                transaction,
            )
        source = await capture.capture(
            CaptureTextCommand(
                access_context=ACCESS_A,
                bot_id=10,
                telegram_update_id=update_id,
                telegram_message_id=update_id + 1000,
                raw_text=f"typed {selection}",
                received_at=NOW,
                trace_id=TRACE_ID,
            ),
            transaction,
        )
    return source.id


async def project_ids(schema_engine: AsyncEngine, model: type[Any]) -> list[UUID]:
    async with create_session_factory(schema_engine)() as session:
        statement = select(model.project_id).order_by(model.project_id)
        return list(await session.scalars(statement))


@pytest.mark.asyncio
async def test_capture_without_current_project_remains_valid_and_unlinked(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    await capture_text(engine, 100)

    async with create_session_factory(schema_engine)() as session:
        source_links = await session.scalar(
            select(func.count()).select_from(ProjectCaptureEventLinkModel)
        )
        note_links = await session.scalar(
            select(func.count()).select_from(ProjectNoteLinkModel)
        )
    assert (source_links, note_links) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["note", "task", "idea", "decision", "question"])
async def test_text_source_and_selected_record_link_to_current_project_once(
    engine: AsyncEngine, schema_engine: AsyncEngine, selection: str
) -> None:
    projects = Projects(PostgresProjectRepository(create_session_factory(engine)))
    project_id = await create_project(projects, ACCESS_A, "Captured project")

    await capture_text(engine, 110, selection)

    source_projects = await project_ids(schema_engine, ProjectCaptureEventLinkModel)
    assert source_projects == [project_id]
    assert await project_ids(schema_engine, LINK_MODELS[selection]) == [project_id]


@pytest.mark.asyncio
async def test_voice_record_inherits_ingress_project_after_current_project_switch(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    captured_project = await create_project(projects, ACCESS_A, "At ingress")
    async with factory() as session, session.begin():
        source = await VoiceCaptureInTransaction().capture(
            CaptureVoiceCommand(
                access_context=ACCESS_A,
                bot_id=10,
                telegram_update_id=200,
                telegram_message_id=1200,
                voice=TelegramVoiceMetadata(
                    file_id="private-file",
                    file_unique_id="private-unique",
                    duration_seconds=2,
                    file_size=12,
                    mime_type="audio/ogg",
                ),
                received_at=NOW,
                trace_id=TRACE_ID,
            ),
            PostgresUpdateTransaction(session),
        )
    await create_project(projects, ACCESS_A, "Selected later")

    processing = PostgresProcessingRepository(factory)
    download = await processing.claim_due_step(
        ACCESS_A,
        NOW,
        timedelta(minutes=15),
        (ProcessingStepType.AUDIO_DOWNLOAD, ProcessingStepType.TRANSCRIPTION),
    )
    assert download is not None
    await VoiceDownloadCompletionInTransaction(factory).complete(
        CompleteVoiceDownloadCommand(
            access_context=ACCESS_A,
            step_id=download.step_id,
            capture_event_id=source.id,
            stored_voice=StoredVoice(
                storage_key=f"{ACCESS_A.user_space_id}/{source.id}/original.ogg",
                local_path="/private/audio.ogg",
                sha256="a" * 64,
                size=12,
                mime_type="audio/ogg",
            ),
            completed_at=NOW + timedelta(seconds=1),
        )
    )
    transcription = await processing.claim_due_step(
        ACCESS_A,
        NOW + timedelta(seconds=1),
        timedelta(minutes=15),
        (ProcessingStepType.AUDIO_DOWNLOAD, ProcessingStepType.TRANSCRIPTION),
    )
    assert transcription is not None
    await VoiceTranscriptionCompletionInTransaction(
        factory, NullConfirmationDelivery(), FixedWorkerIdentity()
    ).complete(
        CompleteVoiceTranscriptionCommand(
            access_context=ACCESS_A,
            step_id=transcription.step_id,
            draft=TranscriptionDraft(
                text="voice note",
                language="en",
                language_probability=0.99,
                model_name="test-model",
                segments=(
                    TranscriptSegment(
                        0.0,
                        1.0,
                        "voice note",
                        (TranscriptWord(0.0, 1.0, "voice note"),),
                    ),
                ),
            ),
            completed_at=NOW + timedelta(seconds=2),
        )
    )

    assert await project_ids(schema_engine, ProjectCaptureEventLinkModel) == [
        captured_project
    ]
    assert await project_ids(schema_engine, ProjectNoteLinkModel) == [captured_project]


@pytest.mark.asyncio
async def test_classifier_record_inherits_all_source_links_not_live_selection(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    factory = create_session_factory(engine)
    projects = Projects(PostgresProjectRepository(factory))
    first_project = await create_project(projects, ACCESS_A, "First")
    source_id = await capture_text(engine, 300)
    second_project = await create_project(projects, ACCESS_A, "Second")
    linked = await PostgresProjectContentLinkRepository(factory).link(
        LinkProjectContentCommand(
            ACCESS_A,
            second_project,
            ProjectContentKind.CAPTURE_EVENT,
            source_id,
            NOW,
            TRACE_ID,
        )
    )
    assert linked is True
    processing = PostgresProcessingRepository(factory)
    claim = await processing.claim_due_step(
        ACCESS_A,
        NOW,
        timedelta(minutes=15),
        (ProcessingStepType.CLASSIFICATION,),
    )
    assert claim is not None
    outcome = ClassificationOutcome(
        source_sha256="b" * 64,
        model_name="test-model",
        prompt_version="test-prompt",
        schema_version="test-schema",
        candidates=(
            GroundedCandidate(
                candidate_type=CandidateType.TASK,
                source_quote="follow up",
                modality=CandidateModality.COMMITMENT,
                confidence=0.9,
                disposition=CandidateDisposition.MATERIALIZE,
                validation_code=CandidateValidationCode.VALID,
            ),
        ),
        discarded_candidate_count=0,
        skipped_reason=None,
    )

    await ClassificationCompletionInTransaction(
        factory, NullConfirmationDelivery(), FixedWorkerIdentity()
    ).complete(
        CompleteClassificationCommand(
            access_context=ACCESS_A,
            step_id=claim.step_id,
            outcome=outcome,
            completed_at=NOW + timedelta(seconds=3),
        )
    )

    assert await project_ids(schema_engine, ProjectTaskLinkModel) == sorted(
        [first_project, second_project]
    )

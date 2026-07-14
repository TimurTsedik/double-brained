from datetime import datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.exact_search_in_transaction import ExactSearchInTransaction
from second_brain.bootstrap.project_context_in_transaction import (
    ProjectContextInTransaction,
)
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.bootstrap.voice_capture_in_transaction import (
    VoiceCaptureInTransaction,
)
from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.capture.application.contracts import TelegramVoiceMetadata
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    TelegramUpdateReceipt,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
    UpdateResult,
)
from second_brain.slices.projects.adapters.persistence.models import (
    ProjectContextModel,
    ProjectModel,
    ProjectTaskLinkModel,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
)
from second_brain.slices.tasks.domain.entities import PendingCaptureType
from tests.projects.conftest import ACCESS_A, NOW


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture(autouse=True)
async def project_telegram_identity(
    reset_project_schema: None, schema_engine: AsyncEngine
) -> None:
    async with schema_engine.begin() as connection:
        await connection.execute(
            insert(TelegramIdentity).values(
                id=UUID("00000000-0000-0000-0000-000000000099"),
                telegram_user_id=42,
                user_id=ACCESS_A.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def callback(update_id: int, data: str, *, private: bool = True) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        private,
        42,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        value,
        telegram_message_id=update_id + 1000,
    )


def voice_update(update_id: int) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        None,
        telegram_message_id=update_id + 1000,
        voice=TelegramVoiceMetadata(
            file_id="private-file",
            file_unique_id="private-unique",
            duration_seconds=2,
            file_size=12,
            mime_type="audio/ogg",
        ),
    )


def processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    task_capture = TaskCaptureInTransaction()
    project_context = ProjectContextInTransaction()
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_text_port=task_capture,
        task_mode_port=task_capture,
        task_panel_port=task_capture,
        exact_search_port=ExactSearchInTransaction(),
        capture_voice_port=VoiceCaptureInTransaction(),
        project_panel_port=project_context,
    )


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


async def create_project_through_telegram(
    app: LocalUpdateProcessor, update_id: int, name: str
) -> UpdateResult:
    await app.process(callback(update_id, "projects:create"))
    return await app.process(text_update(update_id + 1, name))


@pytest.mark.asyncio
async def test_project_name_mode_creates_selects_without_capturing_text(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)

    listed = await app.process(callback(100, "projects:list"))
    prompted = await app.process(callback(101, "projects:create"))
    required = await app.process(text_update(102, "  \n "))
    created_update = text_update(103, "  Second Brain  ")
    created = await app.process(created_update)
    duplicate = await app.process(created_update)

    assert listed.kind is AcknowledgementKind.PROJECTS_LISTED
    assert listed.project_panel is not None
    assert listed.project_panel.items == ()
    assert prompted.kind is AcknowledgementKind.PROJECT_NAME_MODE_SET
    assert required.kind is AcknowledgementKind.PROJECT_NAME_REQUIRED
    assert required.project_panel is not None
    assert required.project_panel.name_required is True
    assert created.kind is AcknowledgementKind.PROJECT_CREATED
    assert created.project_panel is not None
    assert [item.name for item in created.project_panel.items] == ["Second Brain"]
    assert created.project_panel.current_project_id == created.project_panel.items[0].id
    assert duplicate.kind is AcknowledgementKind.PROJECT_CREATED
    assert duplicate.fresh is False
    assert duplicate.project_panel is None
    assert await count(schema_engine, ProjectModel) == 1
    assert await count(schema_engine, CaptureEventModel) == 0
    assert await count(schema_engine, TelegramUpdateReceipt) == 4
    async with create_session_factory(schema_engine)() as session:
        context = await session.scalar(select(ProjectContextModel))
    assert context is not None
    assert context.awaiting_name is False


@pytest.mark.asyncio
async def test_select_clear_and_malformed_project_id_are_safe_and_explicit(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    first = await create_project_through_telegram(app, 200, "First")
    second = await create_project_through_telegram(app, 202, "Second")
    assert first.project_panel is not None
    assert second.project_panel is not None
    first_id = first.project_panel.items[0].id
    second_id = second.project_panel.current_project_id

    await app.process(callback(204, "projects:create"))
    malformed = await app.process(callback(205, "projects:select:not-a-uuid"))
    async with create_session_factory(schema_engine)() as session:
        context_after_malformed = await session.scalar(select(ProjectContextModel))
    selected = await app.process(callback(206, f"projects:select:{first_id}"))
    cleared = await app.process(callback(207, "projects:clear"))

    assert malformed.kind is AcknowledgementKind.PROJECT_SELECTED
    assert malformed.project_panel is not None
    assert malformed.project_panel.action_succeeded is False
    assert malformed.project_panel.current_project_id == second_id
    assert context_after_malformed is not None
    assert context_after_malformed.awaiting_name is False
    assert selected.kind is AcknowledgementKind.PROJECT_SELECTED
    assert selected.project_panel is not None
    assert selected.project_panel.action_succeeded is True
    assert selected.project_panel.current_project_id == first_id
    assert cleared.kind is AcknowledgementKind.PROJECT_CLEARED
    assert cleared.project_panel is not None
    assert cleared.project_panel.action_succeeded is True
    assert cleared.project_panel.current_project_id is None


@pytest.mark.asyncio
async def test_project_creation_mode_is_mutually_exclusive_with_search_and_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(callback(300, "search:prompt"))
    await app.process(callback(301, "capture:idea"))

    prompted = await app.process(callback(302, "projects:create"))
    created = await app.process(text_update(303, "Mode project"))

    assert prompted.kind is AcknowledgementKind.PROJECT_NAME_MODE_SET
    assert created.kind is AcknowledgementKind.PROJECT_CREATED
    assert await count(schema_engine, PendingSearchModeModel) == 0
    assert await count(schema_engine, CaptureEventModel) == 0
    async with create_session_factory(schema_engine)() as session:
        selection = await session.scalar(select(PendingCaptureSelectionModel))
    assert selection is not None
    assert selection.selection is PendingCaptureType.NOTE

    await app.process(callback(304, "projects:create"))
    await app.process(callback(305, "capture:task"))
    captured = await app.process(text_update(306, "linked task"))

    assert captured.kind is AcknowledgementKind.CAPTURED
    assert await count(schema_engine, ProjectModel) == 1
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TaskModel) == 1
    assert await count(schema_engine, ProjectTaskLinkModel) == 1


@pytest.mark.asyncio
async def test_project_callbacks_from_group_or_unknown_route_are_ignored(
    engine: AsyncEngine,
) -> None:
    app = processor(engine)

    group = await app.process(callback(400, "projects:list", private=False))
    unknown = await app.process(callback(401, "projects:delete"))

    assert group.kind is AcknowledgementKind.IGNORED
    assert unknown.kind is AcknowledgementKind.IGNORED


@pytest.mark.asyncio
async def test_voice_cancels_project_name_mode_and_queues_normal_capture(
    engine: AsyncEngine, schema_engine: AsyncEngine
) -> None:
    app = processor(engine)
    await app.process(callback(500, "projects:create"))

    queued = await app.process(voice_update(501))

    assert queued.kind is AcknowledgementKind.VOICE_QUEUED
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, ProjectModel) == 0
    async with create_session_factory(schema_engine)() as session:
        context = await session.scalar(select(ProjectContextModel))
    assert context is not None
    assert context.awaiting_name is False

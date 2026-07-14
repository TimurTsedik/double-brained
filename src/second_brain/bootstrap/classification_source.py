from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.classification.application.contracts import (
    ClassificationSource,
    ReadClassificationSourceCommand,
)
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingRunModel,
    TranscriptModel,
)


class PostgresClassificationSourceReader:
    """Reads the committed source selected by a scoped processing run."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(
        self, command: ReadClassificationSourceCommand
    ) -> ClassificationSource:
        async with self._session_factory() as session:
            async with session.begin():
                await _set_user_space_scope(session, command.access_context)
                raw_text = await session.scalar(
                    select(CaptureEventModel.raw_text)
                    .join(
                        ProcessingRunModel,
                        and_(
                            ProcessingRunModel.capture_event_id == CaptureEventModel.id,
                            ProcessingRunModel.user_space_id
                            == CaptureEventModel.user_space_id,
                        ),
                    )
                    .where(
                        ProcessingRunModel.id == command.processing_run_id,
                        ProcessingRunModel.capture_event_id == command.capture_event_id,
                        ProcessingRunModel.user_space_id
                        == command.access_context.user_space_id,
                        CaptureEventModel.id == command.capture_event_id,
                        CaptureEventModel.user_space_id
                        == command.access_context.user_space_id,
                    )
                )
                source_text = raw_text
                if source_text is None:
                    source_text = await session.scalar(
                        select(TranscriptModel.text)
                        .where(
                            TranscriptModel.processing_run_id
                            == command.processing_run_id,
                            TranscriptModel.capture_event_id
                            == command.capture_event_id,
                            TranscriptModel.user_space_id
                            == command.access_context.user_space_id,
                        )
                        .order_by(TranscriptModel.version.desc())
                        .limit(1)
                    )
                if source_text is None:
                    raise LookupError("classification source was not found")
                return ClassificationSource(
                    text=source_text,
                    base_type=command.base_type,
                )


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

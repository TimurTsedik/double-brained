from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.capture.application.contracts import (
    CaptureImageCommand,
    CaptureTextCommand,
    CaptureVoiceCommand,
    MarkImageStoredCommand,
    MarkVoiceStoredCommand,
    TelegramImageSource,
    TelegramVoiceSource,
)
from second_brain.slices.capture.domain.entities import CaptureEvent, CaptureSourceKind
from second_brain.slices.identity.application.contracts import AccessContext


class PostgresCaptureEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, command: CaptureTextCommand) -> CaptureEvent:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresCaptureEventWriter(session).create(command)

    async def list_recent(self, access_context: AccessContext) -> list[CaptureEvent]:
        async with self._session_factory() as session:
            async with session.begin():
                await _set_user_space_scope(session, access_context)
                models = (
                    await session.scalars(
                        select(CaptureEventModel).order_by(CaptureEventModel.created_at)
                    )
                ).all()
                return [_to_entity(model) for model in models]

    async def count(self, access_context: AccessContext) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                await _set_user_space_scope(session, access_context)
                count = await session.scalar(
                    select(func.count()).select_from(CaptureEventModel)
                )
                return int(count or 0)


class PostgresCaptureEventWriter:
    """Writes one CaptureEvent through a transaction owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, command: CaptureTextCommand) -> CaptureEvent:
        await _set_user_space_scope(self._session, command.access_context)
        model = CaptureEventModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            source_kind=CaptureSourceKind.TEXT,
            channel="telegram",
            bot_id=command.bot_id,
            telegram_update_id=command.telegram_update_id,
            telegram_message_id=command.telegram_message_id,
            raw_text=command.raw_text,
            received_at=command.received_at,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_entity(model)

    async def create_voice(self, command: CaptureVoiceCommand) -> CaptureEvent:
        await _set_user_space_scope(self._session, command.access_context)
        model = CaptureEventModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            source_kind=CaptureSourceKind.VOICE,
            channel="telegram",
            bot_id=command.bot_id,
            telegram_update_id=command.telegram_update_id,
            telegram_message_id=command.telegram_message_id,
            raw_text=None,
            received_at=command.received_at,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )
        attachment = TelegramAttachmentModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            capture_event_id=model.id,
            kind=CaptureSourceKind.VOICE,
            telegram_file_id=command.voice.file_id,
            telegram_file_unique_id=command.voice.file_unique_id,
            duration_seconds=command.voice.duration_seconds,
            width=None,
            height=None,
            telegram_file_size=command.voice.file_size,
            telegram_mime_type=command.voice.mime_type,
            storage_key=None,
            sha256=None,
            stored_size=None,
            stored_mime_type=None,
            stored_at=None,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(attachment)
        await self._session.flush()
        return _to_entity(model)

    async def create_image(self, command: CaptureImageCommand) -> CaptureEvent:
        # Журнал + attachment одним flush'ем: подпись (если есть) хранится в
        # raw_text ДОСЛОВНО, оригинал фото остаётся file_id-метаданными до
        # download-шага воркера (storage_* NULL — «file_id без байтов ≠
        # сохранено»). Telegram у фото mime не отдаёт — sniff'ится на скачивании.
        await _set_user_space_scope(self._session, command.access_context)
        model = CaptureEventModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            source_kind=CaptureSourceKind.IMAGE,
            channel="telegram",
            bot_id=command.bot_id,
            telegram_update_id=command.telegram_update_id,
            telegram_message_id=command.telegram_message_id,
            raw_text=command.caption or None,
            received_at=command.received_at,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )
        attachment = TelegramAttachmentModel(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            capture_event_id=model.id,
            kind=CaptureSourceKind.IMAGE,
            telegram_file_id=command.photo.file_id,
            telegram_file_unique_id=command.photo.file_unique_id,
            duration_seconds=None,
            width=command.photo.width,
            height=command.photo.height,
            telegram_file_size=command.photo.file_size,
            telegram_mime_type=None,
            storage_key=None,
            sha256=None,
            stored_size=None,
            stored_mime_type=None,
            stored_at=None,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )
        self._session.add(model)
        self._session.add(attachment)
        await self._session.flush()
        return _to_entity(model)


class PostgresVoiceSourceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_voice_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramVoiceSource:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresVoiceAttachmentWriter(session).get_voice_source(
                    access_context, capture_event_id
                )

    async def mark_stored(self, command: MarkVoiceStoredCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await PostgresVoiceAttachmentWriter(session).mark_stored(command)


class PostgresVoiceAttachmentWriter:
    """Reads and updates controlled voice fields in a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_voice_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramVoiceSource:
        await _set_user_space_scope(self._session, access_context)
        attachment = await self._session.scalar(
            select(TelegramAttachmentModel).where(
                TelegramAttachmentModel.capture_event_id == capture_event_id,
                TelegramAttachmentModel.user_space_id == access_context.user_space_id,
                TelegramAttachmentModel.kind == CaptureSourceKind.VOICE,
            )
        )
        if attachment is None:
            raise LookupError("voice attachment was not found")
        return TelegramVoiceSource(
            file_id=attachment.telegram_file_id,
            mime_type=attachment.telegram_mime_type,
        )

    async def mark_stored(self, command: MarkVoiceStoredCommand) -> None:
        await _mark_attachment_stored(
            self._session, CaptureSourceKind.VOICE, "voice", command
        )


class PostgresImageSourceRepository:
    """Session-factory обёртка image-attachment'ов для download-шага воркера."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_image_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramImageSource:
        async with self._session_factory() as session:
            async with session.begin():
                return await PostgresImageAttachmentWriter(session).get_image_source(
                    access_context, capture_event_id
                )


class PostgresImageAttachmentWriter:
    """Reads and updates controlled image fields in a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_image_source(
        self, access_context: AccessContext, capture_event_id: UUID
    ) -> TelegramImageSource:
        await _set_user_space_scope(self._session, access_context)
        attachment = await self._session.scalar(
            select(TelegramAttachmentModel).where(
                TelegramAttachmentModel.capture_event_id == capture_event_id,
                TelegramAttachmentModel.user_space_id == access_context.user_space_id,
                TelegramAttachmentModel.kind == CaptureSourceKind.IMAGE,
            )
        )
        if attachment is None:
            raise LookupError("image attachment was not found")
        return TelegramImageSource(file_id=attachment.telegram_file_id)

    async def mark_stored(self, command: MarkImageStoredCommand) -> None:
        await _mark_attachment_stored(
            self._session, CaptureSourceKind.IMAGE, "image", command
        )


async def _mark_attachment_stored(
    session: AsyncSession,
    kind: CaptureSourceKind,
    kind_label: str,
    command: MarkVoiceStoredCommand | MarkImageStoredCommand,
) -> None:
    # Идемпотентный «оригинал скачан»: повтор той же выгрузки — no-op, любая
    # попытка ПЕРЕЗАПИСАТЬ уже сохранённые байты — ошибка (оригинал неизменяем).
    await _set_user_space_scope(session, command.access_context)
    attachment = await session.scalar(
        select(TelegramAttachmentModel)
        .where(
            TelegramAttachmentModel.capture_event_id == command.capture_event_id,
            TelegramAttachmentModel.user_space_id
            == command.access_context.user_space_id,
            TelegramAttachmentModel.kind == kind,
        )
        .with_for_update()
    )
    if attachment is None:
        raise LookupError(f"{kind_label} attachment was not found")
    # Идентичность выгрузки — БЕЗ stored_at: повтор той же выгрузки после
    # истёкшего lease приходит с другим completed_at, и это всё ещё no-op
    # (первый stored_at сохраняется), а не «перезапись».
    current = (
        attachment.storage_key,
        attachment.sha256,
        attachment.stored_size,
        attachment.stored_mime_type,
    )
    expected = (
        command.storage_key,
        command.sha256,
        command.stored_size,
        command.stored_mime_type,
    )
    if current == expected:
        return
    if any(value is not None for value in current):
        raise ValueError(f"{kind_label} attachment storage metadata is immutable")
    (
        attachment.storage_key,
        attachment.sha256,
        attachment.stored_size,
        attachment.stored_mime_type,
    ) = expected
    attachment.stored_at = command.stored_at
    await session.flush()


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_entity(model: CaptureEventModel) -> CaptureEvent:
    return CaptureEvent(
        id=model.id,
        user_space_id=model.user_space_id,
        channel="telegram",
        bot_id=model.bot_id,
        telegram_update_id=model.telegram_update_id,
        telegram_message_id=model.telegram_message_id,
        raw_text=model.raw_text,
        received_at=model.received_at,
        created_at=model.created_at,
        trace_id=model.trace_id,
        source_kind=model.source_kind,
    )

"""S2 «Изображения»: приём фото Telegram-дверью.

Фото с подписью = обычная типизированная запись (текст = caption ДОСЛОВНО) +
immutable CaptureEvent(image) + attachment; фото без подписи = только журнал и
attachment — НИКАКИХ записей-пустышек, пользователь получает честный ack
«📷 Сохранено».
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.types import Update
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.bootstrap.exact_search_in_transaction import ExactSearchInTransaction
from second_brain.bootstrap.image_capture_in_transaction import (
    ImageCaptureInTransaction,
)
from second_brain.bootstrap.schema import reset_prototype_schema
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.identity.adapters.persistence.database import (
    create_session_factory,
)
from second_brain.slices.identity.adapters.persistence.models import (
    TelegramIdentity,
    TelegramUpdateReceipt,
    User,
    UserSpace,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.knowledge.adapters.persistence.models import NoteModel
from second_brain.slices.processing.adapters.persistence.models import (
    ProcessingRunModel,
    ProcessingStepModel,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
)
from second_brain.slices.tasks.adapters.persistence.models import TaskModel
from second_brain.slices.weblinks.adapters.persistence.models import RecordUrlModel
from tests.identity.conftest import IsolatedDatabase
from tests.identity.locale_fakes import FakeLocaleResolver

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture
async def image_database(
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
                language="ru",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await connection.execute(
            insert(TelegramIdentity).values(
                id=UUID("00000000-0000-0000-0000-000000000021"),
                telegram_user_id=42,
                user_id=ACCESS.user_id,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def photo_sizes() -> list[SimpleNamespace]:
    # Telegram шлёт массив PhotoSize; берём МАКСИМАЛЬНОЕ разрешение — порядок
    # в массиве нарочно перепутан, чтобы тест ловил «взяли последний».
    return [
        SimpleNamespace(
            file_id="photo-small",
            file_unique_id="photo-small-unique",
            width=90,
            height=60,
            file_size=1_200,
        ),
        SimpleNamespace(
            file_id="photo-large",
            file_unique_id="photo-large-unique",
            width=1280,
            height=853,
            file_size=222_333,
        ),
        SimpleNamespace(
            file_id="photo-medium",
            file_unique_id="photo-medium-unique",
            width=320,
            height=213,
            file_size=21_000,
        ),
    ]


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def normalize_photo(
    caption: str | None,
    caption_entities: list[SimpleNamespace] | None = None,
) -> TelegramUpdate:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="private"),
        text=None,
        caption=caption,
        caption_entities=caption_entities,
        message_id=200,
        voice=None,
        contact=None,
        entities=None,
        photo=photo_sizes(),
    )
    update = SimpleNamespace(update_id=100, callback_query=None, message=message)
    gateway = AiogramGateway(
        cast(Bot, object()), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    return gateway._normalize(cast(Update, update))


def test_photo_normalization_picks_largest_size_and_hides_file_ids() -> None:
    normalized = normalize_photo("подпись к фото")

    assert normalized.photo is not None
    assert normalized.photo.file_id == "photo-large"
    assert normalized.photo.file_unique_id == "photo-large-unique"
    assert normalized.photo.width == 1280
    assert normalized.photo.height == 853
    assert normalized.photo.file_size == 222_333
    assert normalized.caption == "подпись к фото"
    assert normalized.text is None
    assert "photo-large" not in repr(normalized)
    assert "photo-large-unique" not in repr(normalized)
    assert "подпись к фото" not in repr(normalized)


def test_photo_caption_entities_become_links_with_utf16_offsets() -> None:
    caption = "🔥 смотри доклад тут и ссылку https://example.com/x"
    entities = [
        SimpleNamespace(
            type="text_link",
            offset=utf16_units(caption[: caption.index("тут")]),
            length=utf16_units("тут"),
            url="https://example.com/talk",
        ),
        SimpleNamespace(
            type="url",
            offset=utf16_units(caption[: caption.index("https://example.com/x")]),
            length=utf16_units("https://example.com/x"),
            url=None,
        ),
    ]

    normalized = normalize_photo(caption, entities)

    assert [(link.label, link.url) for link in normalized.links] == [
        ("тут", "https://example.com/talk"),
        ("https://example.com/x", "https://example.com/x"),
    ]


def private_photo(
    update_id: int,
    caption: str | None,
) -> TelegramUpdate:
    from second_brain.slices.capture.application.contracts import (
        TelegramPhotoMetadata,
    )

    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text=None,
        telegram_message_id=update_id + 1_000,
        photo=TelegramPhotoMetadata(
            file_id="private-photo-id",
            file_unique_id="private-photo-unique",
            width=1280,
            height=853,
            file_size=222_333,
        ),
        caption=caption,
    )


def real_processor(engine: AsyncEngine) -> LocalUpdateProcessor:
    task_capture = TaskCaptureInTransaction()
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(create_session_factory(engine)),
        clock=FixedClock(),
        pepper=b"test-pepper",
        pepper_key_id="test-key",
        capture_text_port=task_capture,
        task_mode_port=task_capture,
        task_panel_port=task_capture,
        exact_search_port=ExactSearchInTransaction(),
        capture_image_port=ImageCaptureInTransaction(),
        reminder_ack_port=task_capture,
    )


async def count(schema_engine: AsyncEngine, model: type[object]) -> int:
    async with create_session_factory(schema_engine)() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


@pytest.mark.asyncio
async def test_photo_with_caption_creates_record_links_and_run_in_one_commit(
    image_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    from second_brain.slices.capture.application.contracts import TelegramLink

    update = TelegramUpdate(
        bot_id=1,
        update_id=300,
        is_private=True,
        telegram_user_id=42,
        text=None,
        telegram_message_id=1_300,
        photo=private_photo(300, "смотри тут").photo,
        caption="смотри тут",
        links=(TelegramLink(label="тут", url="https://example.com/talk"),),
    )
    app = real_processor(engine)

    fresh = await app.process(update)
    duplicate = await app.process(update)

    assert fresh.kind is duplicate.kind is AcknowledgementKind.CAPTURED
    assert fresh.fresh is True
    assert duplicate.fresh is False
    assert fresh.trace_id == duplicate.trace_id
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TelegramAttachmentModel) == 1
    assert await count(schema_engine, NoteModel) == 1
    assert await count(schema_engine, RecordUrlModel) == 1
    assert await count(schema_engine, ProcessingRunModel) == 1
    assert await count(schema_engine, TelegramUpdateReceipt) == 1

    async with create_session_factory(schema_engine)() as session:
        source = await session.scalar(select(CaptureEventModel))
        attachment = await session.scalar(select(TelegramAttachmentModel))
        note = await session.scalar(select(NoteModel))
        record_url = await session.scalar(select(RecordUrlModel))
        run = await session.scalar(select(ProcessingRunModel))
        steps = tuple(await session.scalars(select(ProcessingStepModel)))
        target = await session.scalar(select(IndexingTargetModel))
    assert source is not None and attachment is not None
    assert note is not None and run is not None and record_url is not None
    assert target is not None
    assert source.source_kind.value == "image"
    assert source.raw_text == "смотри тут"
    assert attachment.kind.value == "image"
    assert attachment.capture_event_id == source.id
    assert attachment.telegram_file_id == "private-photo-id"
    assert attachment.width == 1280
    assert attachment.height == 853
    assert attachment.duration_seconds is None
    assert attachment.storage_key is None
    assert attachment.sha256 is None
    # Текст записи = подпись ДОСЛОВНО; ссылки — sidecar'ом.
    assert note.text == "смотри тут"
    assert record_url.url == "https://example.com/talk"
    assert record_url.label == "тут"
    assert run.capture_event_id == source.id
    assert run.output_type is TranscriptionOutputType.NOTE
    # Классификация/индексация НЕ гейтятся download'ом: подпись независима от
    # байтов картинки; TRANSCRIPTION в image-run'е нет.
    assert {step.step_type for step in steps} == {
        ProcessingStepType.IMAGE_DOWNLOAD,
        ProcessingStepType.CLASSIFICATION,
        ProcessingStepType.INDEXING,
    }
    assert {step.status for step in steps} == {ProcessingStepStatus.PENDING.value}


@pytest.mark.asyncio
async def test_photo_without_caption_saves_journal_and_file_but_no_record(
    image_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    app = real_processor(engine)

    fresh = await app.process(private_photo(310, None))
    duplicate = await app.process(private_photo(310, None))

    assert fresh.kind is duplicate.kind is AcknowledgementKind.IMAGE_SAVED
    assert fresh.fresh is True
    assert duplicate.fresh is False
    assert await count(schema_engine, CaptureEventModel) == 1
    assert await count(schema_engine, TelegramAttachmentModel) == 1
    # НОЛЬ typed-записей: никаких заметок-пустышек за пользователя.
    assert await count(schema_engine, NoteModel) == 0
    assert await count(schema_engine, TaskModel) == 0
    assert await count(schema_engine, RecordUrlModel) == 0

    async with create_session_factory(schema_engine)() as session:
        source = await session.scalar(select(CaptureEventModel))
        run = await session.scalar(select(ProcessingRunModel))
        steps = tuple(await session.scalars(select(ProcessingStepModel)))
    assert source is not None and run is not None
    assert source.raw_text is None
    # Source-only run: единственный шаг — скачивание оригинала, типа нет.
    assert run.output_type is None
    assert [step.step_type for step in steps] == [ProcessingStepType.IMAGE_DOWNLOAD]


@pytest.mark.asyncio
async def test_photo_with_caption_freezes_selected_type_via_button(
    image_database: None,
    engine: AsyncEngine,
    schema_engine: AsyncEngine,
) -> None:
    app = real_processor(engine)
    selection = TelegramUpdate(
        bot_id=1,
        update_id=320,
        is_private=True,
        telegram_user_id=42,
        text=None,
        callback_query_id="selection-callback",
        callback_data="capture:task",
    )

    await app.process(selection)
    result = await app.process(private_photo(321, "оплатить счёт"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert await count(schema_engine, TaskModel) == 1
    async with create_session_factory(schema_engine)() as session:
        task = await session.scalar(select(TaskModel))
        run = await session.scalar(select(ProcessingRunModel))
    assert task is not None and run is not None
    assert task.title == "оплатить счёт"
    assert run.output_type is TranscriptionOutputType.TASK


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_gateway_sends_honest_image_saved_acknowledgement() -> None:
    bot = RecordingBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_acknowledgement(
        private_photo(330, None), AcknowledgementKind.IMAGE_SAVED
    )

    assert bot.messages == [(42, "📷 Сохранено")]


class RecordViewBot(RecordingBot):
    def __init__(self, fail_photo_by_file_id: bool = False) -> None:
        super().__init__()
        self.photos: list[tuple[int, object, str | None]] = []
        self._fail_photo_by_file_id = fail_photo_by_file_id

    async def send_message(
        self, chat_id: int, text: str, reply_markup: object | None = None
    ) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(
        self, chat_id: int, photo: object, caption: str | None = None
    ) -> None:
        if self._fail_photo_by_file_id and isinstance(photo, str):
            raise RuntimeError("telegram rejected the stale file_id")
        self.photos.append((chat_id, photo, caption))


@pytest.mark.asyncio
async def test_show_full_marks_image_sourced_record_under_the_text() -> None:
    from second_brain.slices.retrieval.application.contracts import (
        RecordViewResult,
    )
    from second_brain.slices.retrieval.domain.entities import (
        RecordView,
        SearchRecordType,
    )

    bot = RecordViewBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    record = RecordView(
        id=UUID("00000000-0000-0000-0000-000000000501"),
        record_type=SearchRecordType.NOTE,
        text="подпись к фото",
        created_at=NOW,
        task_completed=None,
        has_image_source=True,
    )

    await gateway.send_record_view(
        private_photo(340, None),
        RecordViewResult(record=record, related=(), links=()),
    )

    assert len(bot.messages) == 1
    _chat, sent = bot.messages[0]
    # Текст записи дословный, пометка — отдельной строкой ПОД ним.
    assert "подпись к фото\n\n📷 изображение сохранено" in sent


def _image_record(text: str) -> object:
    from second_brain.slices.retrieval.domain.entities import (
        RecordView,
        SearchRecordType,
    )

    return RecordView(
        id=UUID("00000000-0000-0000-0000-000000000503"),
        record_type=SearchRecordType.NOTE,
        text=text,
        created_at=NOW,
        task_completed=None,
        has_image_source=True,
    )


@pytest.mark.asyncio
async def test_show_full_sends_the_photo_by_file_id_after_the_text() -> None:
    from second_brain.slices.retrieval.application.contracts import (
        RecordImageSource,
        RecordViewResult,
    )
    from second_brain.slices.retrieval.domain.entities import RecordView

    bot = RecordViewBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_record_view(
        private_photo(350, None),
        RecordViewResult(
            record=cast(RecordView, _image_record("подпись к фото")),
            related=(),
            links=(),
            image=RecordImageSource(
                telegram_file_id="stored-file-id",
                local_path="/private/images/original.jpg",
            ),
        ),
    )

    # Текст ушёл ПЕРВЫМ, фото — дополнительным сообщением после него.
    assert len(bot.messages) == 1
    assert "подпись к фото" in bot.messages[0][1]
    assert bot.photos == [(42, "stored-file-id", "📷 источник записи")]


@pytest.mark.asyncio
async def test_show_full_falls_back_to_stored_bytes_when_file_id_dies() -> None:
    from aiogram.types import FSInputFile

    from second_brain.slices.retrieval.application.contracts import (
        RecordImageSource,
        RecordViewResult,
    )
    from second_brain.slices.retrieval.domain.entities import RecordView

    bot = RecordViewBot(fail_photo_by_file_id=True)
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_record_view(
        private_photo(351, None),
        RecordViewResult(
            record=cast(RecordView, _image_record("подпись")),
            related=(),
            links=(),
            image=RecordImageSource(
                telegram_file_id="dead-file-id",
                local_path="/private/images/original.jpg",
            ),
        ),
    )

    # file_id отвергнут → байты из хранилища (источник истины — файл).
    assert len(bot.photos) == 1
    _chat, photo, caption = bot.photos[0]
    assert isinstance(photo, FSInputFile)
    assert photo.path == "/private/images/original.jpg"
    assert caption == "📷 источник записи"


@pytest.mark.asyncio
async def test_show_full_without_stored_bytes_keeps_only_the_text_mark() -> None:
    from second_brain.slices.retrieval.application.contracts import (
        RecordImageSource,
        RecordViewResult,
    )
    from second_brain.slices.retrieval.domain.entities import RecordView

    bot = RecordViewBot(fail_photo_by_file_id=True)
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    # Байты ещё не скачаны (local_path None), file_id мёртв → фото нет, но
    # показ НЕ падает и честная пометка в тексте остаётся.
    await gateway.send_record_view(
        private_photo(352, None),
        RecordViewResult(
            record=cast(RecordView, _image_record("подпись")),
            related=(),
            links=(),
            image=RecordImageSource(telegram_file_id="dead-file-id", local_path=None),
        ),
    )

    assert bot.photos == []
    assert len(bot.messages) == 1
    assert "📷 изображение сохранено" in bot.messages[0][1]


@pytest.mark.asyncio
async def test_lists_mark_image_sourced_records_with_a_camera() -> None:
    from second_brain.slices.retrieval.application.contracts import (
        RecordViewResult,
        SearchPanelResult,
    )
    from second_brain.slices.retrieval.domain.entities import (
        MatchQuality,
        RecordView,
        SearchRecord,
        SearchRecordType,
    )

    bot = RecordViewBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    update = private_photo(360, None)

    def search_item(index: int, has_image: bool) -> SearchRecord:
        return SearchRecord(
            id=UUID(f"00000000-0000-0000-0000-00000000060{index}"),
            record_type=SearchRecordType.NOTE,
            text=f"результат {index}",
            source_capture_event_id=UUID(f"10000000-0000-0000-0000-00000000060{index}"),
            created_at=NOW,
            task_completed=None,
            match_quality=MatchQuality.FULL_TEXT,
            has_image_source=has_image,
        )

    await gateway.send_search_panel(
        update,
        SearchPanelResult(
            items=(search_item(1, True), search_item(2, False)), query_required=False
        ),
    )
    search_text = bot.messages[-1][1]
    assert "1. 📝 Заметка 📷\n" in search_text
    assert "2. 📝 Заметка\n" in search_text

    # «Похожее» под показанной записью — те же метки.
    related_item = RecordView(
        id=UUID("00000000-0000-0000-0000-000000000611"),
        record_type=SearchRecordType.NOTE,
        text="похожая из фото",
        created_at=NOW,
        task_completed=None,
        has_image_source=True,
    )
    plain_record = RecordView(
        id=UUID("00000000-0000-0000-0000-000000000612"),
        record_type=SearchRecordType.NOTE,
        text="показанная запись",
        created_at=NOW,
        task_completed=None,
    )
    await gateway.send_record_view(
        update,
        RecordViewResult(record=plain_record, related=(related_item,), links=()),
    )
    related_text = bot.messages[-1][1]
    assert "1. 📝 Заметка 📷\n" in related_text


@pytest.mark.asyncio
async def test_digest_rows_mark_image_sourced_records() -> None:
    from second_brain.slices.retrieval.application.contracts import DigestPage
    from second_brain.slices.retrieval.domain.entities import (
        DigestCounters,
        DigestPeriod,
        RecordView,
        SearchRecordType,
    )

    bot = RecordViewBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    items = (
        RecordView(
            id=UUID("00000000-0000-0000-0000-000000000621"),
            record_type=SearchRecordType.NOTE,
            text="запись из фото",
            created_at=NOW,
            task_completed=None,
            has_image_source=True,
        ),
        RecordView(
            id=UUID("00000000-0000-0000-0000-000000000622"),
            record_type=SearchRecordType.NOTE,
            text="обычная запись",
            created_at=NOW,
            task_completed=None,
        ),
    )

    await gateway.send_digest(
        private_photo(361, None),
        DigestPage(
            period=DigestPeriod.WEEK,
            period_start=NOW,
            as_of=NOW,
            offset=0,
            total=2,
            counters=DigestCounters(2, 0, 0, 0, 0, 0),
            items=items,
        ),
    )

    digest_text = bot.messages[-1][1]
    assert "1. 📝 Заметка 📷 · " in digest_text
    assert "2. 📝 Заметка · " in digest_text


@pytest.mark.asyncio
async def test_show_full_has_no_image_mark_for_text_sourced_record() -> None:
    from second_brain.slices.retrieval.application.contracts import (
        RecordViewResult,
    )
    from second_brain.slices.retrieval.domain.entities import (
        RecordView,
        SearchRecordType,
    )

    bot = RecordViewBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    record = RecordView(
        id=UUID("00000000-0000-0000-0000-000000000502"),
        record_type=SearchRecordType.NOTE,
        text="обычная заметка",
        created_at=NOW,
        task_completed=None,
    )

    await gateway.send_record_view(
        private_photo(341, None),
        RecordViewResult(record=record, related=(), links=()),
    )

    assert len(bot.messages) == 1
    assert "📷" not in bot.messages[0][1]


def test_contained_image_path_rejects_escapes_from_the_storage_root(
    tmp_path: Path,
) -> None:
    # storage_key из БД обязан остаться ВНУТРИ корня: абсолютный путь или
    # «../» в испорченной строке = отправка произвольного файла с диска.
    from second_brain.bootstrap.record_view_in_transaction import (
        contained_image_path,
    )

    root = str(tmp_path)
    good = contained_image_path(root, "space/capture/original.jpg")
    assert good is not None
    assert good.startswith(str(tmp_path.resolve()))

    assert contained_image_path(root, "/etc/passwd") is None
    assert contained_image_path(root, "../../etc/passwd") is None
    assert contained_image_path(root, "space/../../outside.jpg") is None

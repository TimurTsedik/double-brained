from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot

from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
    UpdateResult,
)
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    StoredUpdateReceipt,
)
from second_brain.slices.retrieval.application.contracts import (
    ConsumeSearchQueryCommand,
    SearchPanelResult,
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.domain.entities import (
    MatchQuality,
    SearchRecord,
    SearchRecordType,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
)
from tests.identity.locale_fakes import FakeLocaleResolver

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
SEARCH_RECORD = SearchRecord(
    id=UUID("00000000-0000-0000-0000-000000000301"),
    record_type=SearchRecordType.NOTE,
    text="private result",
    source_capture_event_id=UUID("00000000-0000-0000-0000-000000000401"),
    created_at=NOW,
    task_completed=None,
    match_quality=MatchQuality.SUBSTRING,
)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class KnownActorStore:
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        handler: object,
    ) -> StoredUpdateReceipt:
        result = await handler(self)
        assert isinstance(result, NewUpdateResult)
        return StoredUpdateReceipt(
            result.result_kind,
            result.trace_id,
            existing=False,
            span_id=result.span_id,
        )

    async def resolve_access_context(self, _telegram_user_id: int) -> AccessContext:
        return ACCESS

    async def read_user_space_language(
        self, _access_context: AccessContext
    ) -> str | None:
        return "ru"


class DuplicateSearchStore(KnownActorStore):
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        _handler: object,
    ) -> StoredUpdateReceipt:
        return StoredUpdateReceipt(
            "search_completed",
            "1" * 32,
            existing=True,
        )


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(
        self, _telegram_user_id: int
    ) -> AccessContext | None:
        return None


class RecordingCapturePort:
    def __init__(self) -> None:
        self.commands: list[CaptureTextCommand] = []

    async def capture(
        self, command: CaptureTextCommand, _transaction: object
    ) -> CaptureEvent:
        self.commands.append(command)
        return CaptureEvent(
            id=UUID("00000000-0000-0000-0000-000000000501"),
            user_space_id=command.access_context.user_space_id,
            channel="telegram",
            bot_id=command.bot_id,
            telegram_update_id=command.telegram_update_id,
            telegram_message_id=command.telegram_message_id,
            raw_text=command.raw_text,
            received_at=command.received_at,
            created_at=command.received_at,
            trace_id=command.trace_id,
        )


class RecordingTaskModePort:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.selections: list[SetPendingCaptureSelectionCommand] = []

    async def set_awaiting_task(
        self, _command: SetAwaitingTaskCommand, _transaction: object
    ) -> None:
        self.operations.append("task:set")

    async def set_selection(
        self, command: SetPendingCaptureSelectionCommand, _transaction: object
    ) -> None:
        self.operations.append("task:selection")
        self.selections.append(command)

    async def cancel(
        self, _command: CancelPendingTaskCommand, _transaction: object
    ) -> None:
        self.operations.append("task:cancel")


class RecordingSearchPort:
    def __init__(
        self,
        operations: list[str],
        consume_result: SearchPanelResult | None = None,
    ) -> None:
        self.operations = operations
        self.consume_result = consume_result
        self.set_commands: list[SetAwaitingSearchCommand] = []
        self.cancelled: list[AccessContext] = []
        self.query_commands: list[ConsumeSearchQueryCommand] = []

    async def set_awaiting(
        self, command: SetAwaitingSearchCommand, _transaction: object
    ) -> None:
        self.operations.append("search:set")
        self.set_commands.append(command)

    async def cancel(self, access_context: AccessContext, _transaction: object) -> None:
        self.operations.append("search:cancel")
        self.cancelled.append(access_context)

    async def consume_query(
        self, command: ConsumeSearchQueryCommand, _transaction: object
    ) -> SearchPanelResult | None:
        self.operations.append("search:consume")
        self.query_commands.append(command)
        return self.consume_result


def callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
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
        telegram_message_id=update_id + 1_000,
    )


@pytest.mark.asyncio
async def test_search_prompt_resets_capture_mode_before_setting_search() -> None:
    operations: list[str] = []
    task_mode = RecordingTaskModePort(operations)
    search = RecordingSearchPort(operations)
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        task_mode_port=task_mode,
        exact_search_port=search,
    )

    result = await processor.process(callback(200, "search:prompt"))

    assert result.kind is AcknowledgementKind.SEARCH_MODE_SET
    assert operations == ["task:cancel", "search:set"]
    assert search.set_commands == [
        SetAwaitingSearchCommand(ACCESS, NOW, result.trace_id)
    ]


@pytest.mark.asyncio
async def test_capture_selection_cancels_search_before_selecting_type() -> None:
    operations: list[str] = []
    task_mode = RecordingTaskModePort(operations)
    search = RecordingSearchPort(operations)
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        task_mode_port=task_mode,
        exact_search_port=search,
    )

    result = await processor.process(callback(201, "capture:idea"))

    assert result.kind is AcknowledgementKind.TASK_MODE_SET
    assert operations == ["search:cancel", "task:selection"]
    assert task_mode.selections[0].selection == "idea"


@pytest.mark.asyncio
async def test_search_cancel_clears_pending_mode() -> None:
    operations: list[str] = []
    search = RecordingSearchPort(operations)
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        exact_search_port=search,
    )

    result = await processor.process(callback(202, "search:cancel"))

    assert result.kind is AcknowledgementKind.SEARCH_MODE_CANCELLED
    assert operations == ["search:cancel"]
    assert search.cancelled == [ACCESS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("panel", "expected_kind"),
    [
        (
            SearchPanelResult((SEARCH_RECORD,), query_required=False),
            AcknowledgementKind.SEARCH_COMPLETED,
        ),
        (
            SearchPanelResult((), query_required=True),
            AcknowledgementKind.SEARCH_QUERY_REQUIRED,
        ),
    ],
)
async def test_pending_search_consumes_text_without_capture(
    panel: SearchPanelResult, expected_kind: AcknowledgementKind
) -> None:
    operations: list[str] = []
    capture = RecordingCapturePort()
    search = RecordingSearchPort(operations, panel)
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        capture_text_port=capture,
        exact_search_port=search,
    )

    result = await processor.process(text_update(203, "private query"))

    assert result.kind is expected_kind
    assert result.search_panel == panel
    assert capture.commands == []
    assert search.query_commands == [ConsumeSearchQueryCommand(ACCESS, "private query")]


@pytest.mark.asyncio
async def test_text_without_pending_search_uses_existing_capture_path() -> None:
    operations: list[str] = []
    capture = RecordingCapturePort()
    search = RecordingSearchPort(operations, None)
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        capture_text_port=capture,
        exact_search_port=search,
    )

    result = await processor.process(text_update(204, "ordinary note"))

    assert result.kind is AcknowledgementKind.CAPTURED
    assert operations == ["search:consume"]
    assert len(capture.commands) == 1


@pytest.mark.asyncio
async def test_duplicate_search_result_has_no_transient_payload() -> None:
    operations: list[str] = []
    search = RecordingSearchPort(operations)
    processor = LocalUpdateProcessor(
        DuplicateSearchStore(),
        FixedClock(),
        b"pepper",
        "key",
        exact_search_port=search,
    )

    result = await processor.process(text_update(205, "private query"))

    assert result.kind is AcknowledgementKind.SEARCH_COMPLETED
    assert result.fresh is False
    assert result.search_panel is None
    assert operations == []


@pytest.mark.asyncio
async def test_search_prompt_from_unknown_actor_is_ignored() -> None:
    operations: list[str] = []
    processor = LocalUpdateProcessor(
        UnknownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        task_mode_port=RecordingTaskModePort(operations),
        exact_search_port=RecordingSearchPort(operations),
    )

    result = await processor.process(callback(206, "search:prompt"))

    assert result.kind is AcknowledgementKind.IGNORED
    assert operations == []


@pytest.mark.asyncio
async def test_search_prompt_from_group_chat_is_ignored() -> None:
    operations: list[str] = []
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        task_mode_port=RecordingTaskModePort(operations),
        exact_search_port=RecordingSearchPort(operations),
    )
    group_callback = TelegramUpdate(
        1,
        207,
        False,
        42,
        None,
        callback_query_id="callback-207",
        callback_data="search:prompt",
    )

    result = await processor.process(group_callback)

    assert result.kind is AcknowledgementKind.IGNORED
    assert operations == []


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)


def search_record(
    record_number: int,
    record_type: SearchRecordType,
    text: str,
    *,
    task_completed: bool | None = None,
) -> SearchRecord:
    return SearchRecord(
        id=UUID(int=record_number),
        record_type=record_type,
        text=text,
        source_capture_event_id=UUID(int=record_number + 100),
        created_at=NOW,
        task_completed=task_completed,
        match_quality=MatchQuality.SUBSTRING,
    )


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_search_prompt_with_cancel_button() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_search_prompt(
        callback(300, "search:prompt"), query_required=False
    )

    message = bot.sent_messages[0]
    assert message["text"] == (
        "🔎 Что найти?\n\n"
        "Отправьте слово или фразу. Следующее сообщение станет запросом, "
        "а не новой записью."
    )
    assert "parse_mode" not in message
    markup = message["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[0]] == ["✖️ Отмена"]
    assert [button.callback_data for button in markup.inline_keyboard[0]] == [
        "search:cancel"
    ]


@pytest.mark.asyncio
async def test_aiogram_gateway_reprompts_for_blank_search_query() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_search_prompt(text_update(301, "  "), query_required=True)

    assert bot.sent_messages[0]["text"] == "Напишите слово или фразу."
    assert (
        bot.sent_messages[0]["reply_markup"].inline_keyboard[0][0].callback_data
        == "search:cancel"
    )


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_search_cancelled() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_search_cancelled(callback(302, "search:cancel"))

    assert bot.sent_messages == [{"chat_id": 42, "text": "✖️ Поиск отменён."}]


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_compact_typed_search_results() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    long_note = "  PostgreSQL\n" + "x" * 300
    result = SearchPanelResult(
        (
            search_record(1, SearchRecordType.NOTE, long_note),
            search_record(
                2,
                SearchRecordType.TASK,
                "Открытая задача",
                task_completed=False,
            ),
            search_record(
                3,
                SearchRecordType.TASK,
                "Закрытая задача",
                task_completed=True,
            ),
            search_record(4, SearchRecordType.IDEA, "Идея"),
            search_record(5, SearchRecordType.DECISION, "Решение"),
            search_record(6, SearchRecordType.QUESTION, "Вопрос"),
        ),
        query_required=False,
    )

    await gateway.send_search_panel(text_update(303, "postgres"), result)

    message = bot.sent_messages[0]
    assert message["text"].startswith("🔎 Найдено: 6\n\n1. 📝 Заметка\n")
    assert "  PostgreSQL\n" not in message["text"]
    assert len(message["text"].splitlines()[3]) == 240
    assert "…\n\n2. ✅ Задача\nОткрытая задача" in message["text"]
    assert "3. ☑️ Завершённая задача\nЗакрытая задача" in message["text"]
    assert "4. 💡 Идея\nИдея" in message["text"]
    assert "5. ⚖️ Решение\nРешение" in message["text"]
    assert "6. ❓ Вопрос\nВопрос" in message["text"]
    assert "parse_mode" not in message
    markup = message["reply_markup"]
    # Номерные кнопки «1…N» открывают записи целиком; «Искать ещё» — последний ряд.
    number_buttons = [button for row in markup.inline_keyboard[:-1] for button in row]
    assert [button.text for button in number_buttons] == ["1", "2", "3", "4", "5", "6"]
    assert [button.callback_data for button in number_buttons] == [
        f"show:{item.record_type.value}:{item.id}" for item in result.items
    ]
    assert [button.callback_data for button in markup.inline_keyboard[-1]] == [
        "search:prompt"
    ]


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_exact_empty_search_result() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )

    await gateway.send_search_panel(
        text_update(304, "missing"),
        SearchPanelResult((), query_required=False),
    )

    message = bot.sent_messages[0]
    assert message["text"] == (
        "🔎 Ничего не найдено.\n\nПопробуйте другое слово или более короткую фразу."
    )
    assert (
        message["reply_markup"].inline_keyboard[0][0].callback_data == "search:prompt"
    )


@pytest.mark.asyncio
async def test_ten_maximum_search_excerpts_fit_telegram_message() -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver()
    )
    result = SearchPanelResult(
        tuple(
            search_record(number, SearchRecordType.NOTE, "x" * 500)
            for number in range(1, 11)
        ),
        query_required=False,
    )

    await gateway.send_search_panel(text_update(305, "x"), result)

    assert len(bot.sent_messages[0]["text"]) < 4096


class AcquiredPollerLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


class StaticSearchProcessor:
    def __init__(self, result: UpdateResult) -> None:
        self._result = result

    async def process(self, _update: TelegramUpdate) -> UpdateResult:
        return self._result


class SearchGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.answered_callbacks: list[TelegramUpdate] = []
        self.prompts: list[tuple[TelegramUpdate, bool]] = []
        self.cancelled: list[TelegramUpdate] = []
        self.panels: list[tuple[TelegramUpdate, SearchPanelResult]] = []
        self.acknowledgements: list[AcknowledgementKind] = []

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        return [self._update]

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.answered_callbacks.append(update)

    async def send_search_prompt(
        self, update: TelegramUpdate, query_required: bool
    ) -> None:
        self.prompts.append((update, query_required))

    async def send_search_cancelled(self, update: TelegramUpdate) -> None:
        self.cancelled.append(update)

    async def send_search_panel(
        self, update: TelegramUpdate, result: SearchPanelResult
    ) -> None:
        self.panels.append((update, result))

    async def send_acknowledgement(
        self, _update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.acknowledgements.append(kind)


class FailsOnceSearchPanelGateway(SearchGateway):
    def __init__(self, update: TelegramUpdate) -> None:
        super().__init__(update)
        self.panel_attempts = 0

    async def send_search_panel(
        self, update: TelegramUpdate, result: SearchPanelResult
    ) -> None:
        self.panel_attempts += 1
        if self.panel_attempts == 1:
            raise RuntimeError("Telegram search panel send failed")
        await super().send_search_panel(update, result)


async def no_sleep(_seconds: float) -> None:
    return None


def search_update_result(
    kind: AcknowledgementKind,
    *,
    fresh: bool = True,
    panel: SearchPanelResult | None = None,
) -> UpdateResult:
    return UpdateResult(
        kind,
        "1" * 32,
        "2" * 16,
        fresh=fresh,
        search_panel=panel,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "query_required"),
    [
        (AcknowledgementKind.SEARCH_MODE_SET, False),
        (AcknowledgementKind.SEARCH_QUERY_REQUIRED, True),
    ],
)
async def test_poller_sends_fresh_search_prompt(
    kind: AcknowledgementKind, query_required: bool
) -> None:
    update = callback(400, "search:prompt")
    gateway = SearchGateway(update)

    await LocalPoller(
        gateway,
        StaticSearchProcessor(search_update_result(kind)),
        AcquiredPollerLock(),
    ).run_once()

    assert gateway.prompts == [(update, query_required)]
    assert gateway.acknowledgements == []


@pytest.mark.asyncio
async def test_poller_sends_fresh_search_cancelled() -> None:
    update = callback(401, "search:cancel")
    gateway = SearchGateway(update)

    await LocalPoller(
        gateway,
        StaticSearchProcessor(
            search_update_result(AcknowledgementKind.SEARCH_MODE_CANCELLED)
        ),
        AcquiredPollerLock(),
    ).run_once()

    assert gateway.cancelled == [update]
    assert gateway.acknowledgements == []


@pytest.mark.asyncio
async def test_poller_sends_fresh_search_panel_and_not_duplicate() -> None:
    update = text_update(402, "postgres")
    panel = SearchPanelResult((SEARCH_RECORD,), query_required=False)
    fresh_gateway = SearchGateway(update)

    await LocalPoller(
        fresh_gateway,
        StaticSearchProcessor(
            search_update_result(AcknowledgementKind.SEARCH_COMPLETED, panel=panel)
        ),
        AcquiredPollerLock(),
    ).run_once()

    assert fresh_gateway.panels == [(update, panel)]
    assert fresh_gateway.acknowledgements == []

    duplicate_gateway = SearchGateway(update)
    await LocalPoller(
        duplicate_gateway,
        StaticSearchProcessor(
            search_update_result(
                AcknowledgementKind.SEARCH_COMPLETED,
                fresh=False,
            )
        ),
        AcquiredPollerLock(),
    ).run_once()

    assert duplicate_gateway.panels == []
    assert duplicate_gateway.acknowledgements == []


@pytest.mark.asyncio
async def test_poller_retries_search_panel_before_advancing_offset() -> None:
    update = text_update(403, "postgres")
    panel = SearchPanelResult((SEARCH_RECORD,), query_required=False)
    gateway = FailsOnceSearchPanelGateway(update)
    poller = LocalPoller(
        gateway,
        StaticSearchProcessor(
            search_update_result(AcknowledgementKind.SEARCH_COMPLETED, panel=panel)
        ),
        AcquiredPollerLock(),
        sleep=no_sleep,
    )

    await poller.run_once()

    assert gateway.panel_attempts == 2
    assert gateway.panels == [(update, panel)]
    assert poller.offset == update.update_id + 1

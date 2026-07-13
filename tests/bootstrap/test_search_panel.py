from datetime import UTC, datetime
from uuid import UUID

import pytest

from second_brain.slices.capture.application.contracts import CaptureTextCommand
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
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

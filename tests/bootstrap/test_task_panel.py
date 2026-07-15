from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    StoredUpdateReceipt,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    CompleteTaskCommand,
    SetAwaitingTaskCommand,
    TaskListItem,
    TaskPanelResult,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
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


@pytest.mark.asyncio
async def test_known_private_start_returns_panel_shown() -> None:
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
    )

    result = await processor.process(
        TelegramUpdate(
            bot_id=1,
            update_id=100,
            is_private=True,
            telegram_user_id=42,
            text="/start",
        )
    )

    assert result.kind is AcknowledgementKind.PANEL_SHOWN


class PanelGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.panels: list[TelegramUpdate] = []
        self.selection_feedback: list[TelegramUpdate] = []
        self.task_panels: list[tuple[TelegramUpdate, TaskPanelResult, bool]] = []
        self.answered_callbacks: list[TelegramUpdate] = []
        self.acknowledgements: list[AcknowledgementKind] = []

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        return [self._update]

    async def send_panel(self, update: TelegramUpdate) -> None:
        self.panels.append(update)

    async def send_selection_feedback(self, update: TelegramUpdate) -> None:
        self.selection_feedback.append(update)

    async def send_task_panel(
        self,
        update: TelegramUpdate,
        result: TaskPanelResult,
        is_completion: bool,
    ) -> None:
        self.task_panels.append((update, result, is_completion))

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.answered_callbacks.append(update)

    async def send_acknowledgement(
        self, _update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.acknowledgements.append(kind)


class AcquiredPollerLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


class StaticProcessor:
    def __init__(self, result: object) -> None:
        self._result = result

    async def process(self, _update: TelegramUpdate) -> object:
        return self._result


class FailsOnceProcessor:
    def __init__(self, gateway: PanelGateway, result: object) -> None:
        self._gateway = gateway
        self._result = result
        self.answer_counts_at_processing: list[int] = []

    async def process(self, _update: TelegramUpdate) -> object:
        self.answer_counts_at_processing.append(len(self._gateway.answered_callbacks))
        if len(self.answer_counts_at_processing) == 1:
            raise RuntimeError("durable callback processing failed")
        return self._result


class FailsOncePanelGateway(PanelGateway):
    def __init__(self, update: TelegramUpdate) -> None:
        super().__init__(update)
        self.panel_attempts = 0

    async def send_panel(self, update: TelegramUpdate) -> None:
        self.panel_attempts += 1
        if self.panel_attempts == 1:
            raise RuntimeError("Telegram panel send failed")
        await super().send_panel(update)


class FailsOnceTaskPanelGateway(PanelGateway):
    def __init__(self, update: TelegramUpdate) -> None:
        super().__init__(update)
        self.task_panel_attempts = 0

    async def send_task_panel(
        self,
        update: TelegramUpdate,
        result: TaskPanelResult,
        is_completion: bool,
    ) -> None:
        self.task_panel_attempts += 1
        if self.task_panel_attempts == 1:
            raise RuntimeError("Telegram task panel send failed")
        await super().send_task_panel(update, result, is_completion)


async def no_sleep(_seconds: float) -> None:
    return None


def update_result(
    kind: AcknowledgementKind,
    fresh: bool,
    task_panel: TaskPanelResult | None = None,
) -> object:
    return type(
        "Result",
        (),
        {"kind": kind, "fresh": fresh, "task_panel": task_panel},
    )()


@pytest.mark.asyncio
async def test_fresh_panel_result_sends_one_panel_and_duplicate_sends_none() -> None:
    update = TelegramUpdate(1, 101, True, 42, "/start")
    fresh_gateway = PanelGateway(update)

    await LocalPoller(
        fresh_gateway,
        StaticProcessor(update_result(AcknowledgementKind.PANEL_SHOWN, True)),
        AcquiredPollerLock(),
    ).run_once()

    assert fresh_gateway.panels == [update]
    assert fresh_gateway.acknowledgements == []

    duplicate_gateway = PanelGateway(update)
    await LocalPoller(
        duplicate_gateway,
        StaticProcessor(update_result(AcknowledgementKind.PANEL_SHOWN, False)),
        AcquiredPollerLock(),
    ).run_once()

    assert duplicate_gateway.panels == []
    assert duplicate_gateway.acknowledgements == []


@pytest.mark.asyncio
async def test_callback_always_closes_spinner_without_chat_message() -> None:
    update = TelegramUpdate(
        1,
        102,
        True,
        42,
        None,
        callback_query_id="callback-102",
        callback_data="task:await_text",
    )
    gateway = PanelGateway(update)

    await LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.TASK_MODE_SET, True)),
        AcquiredPollerLock(),
    ).run_once()

    assert gateway.answered_callbacks == [update]
    assert gateway.panels == []
    assert gateway.acknowledgements == []


@pytest.mark.asyncio
async def test_fresh_note_selection_sends_feedback_once_and_duplicate_sends_none() -> (
    None
):
    update = private_callback(103, "capture:note")
    fresh_gateway = PanelGateway(update)

    await LocalPoller(
        fresh_gateway,
        StaticProcessor(update_result(AcknowledgementKind.TASK_MODE_SET, True)),
        AcquiredPollerLock(),
    ).run_once()

    assert fresh_gateway.selection_feedback == [update]

    duplicate_gateway = PanelGateway(update)
    await LocalPoller(
        duplicate_gateway,
        StaticProcessor(update_result(AcknowledgementKind.TASK_MODE_SET, False)),
        AcquiredPollerLock(),
    ).run_once()

    assert duplicate_gateway.selection_feedback == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "is_completion"),
    [
        (AcknowledgementKind.TASKS_LISTED, False),
        (AcknowledgementKind.TASK_COMPLETED, True),
    ],
)
async def test_fresh_task_action_sends_panel_once(
    kind: AcknowledgementKind, is_completion: bool
) -> None:
    update = private_callback(106, "tasks:list")
    task_panel = TaskPanelResult(items=(), completion_changed=is_completion)
    fresh_gateway = PanelGateway(update)

    await LocalPoller(
        fresh_gateway,
        StaticProcessor(update_result(kind, True, task_panel)),
        AcquiredPollerLock(),
    ).run_once()

    assert fresh_gateway.task_panels == [(update, task_panel, is_completion)]
    assert fresh_gateway.acknowledgements == []

    duplicate_gateway = PanelGateway(update)
    await LocalPoller(
        duplicate_gateway,
        StaticProcessor(update_result(kind, False, None)),
        AcquiredPollerLock(),
    ).run_once()

    assert duplicate_gateway.task_panels == []


@pytest.mark.asyncio
async def test_callback_spinner_is_answered_before_a_processor_retry() -> None:
    update = TelegramUpdate(
        1,
        104,
        True,
        42,
        None,
        callback_query_id="callback-104",
        callback_data="task:await_text",
    )
    gateway = PanelGateway(update)
    processor = FailsOnceProcessor(
        gateway,
        update_result(AcknowledgementKind.TASK_MODE_SET, True),
    )

    await LocalPoller(
        gateway,
        processor,
        AcquiredPollerLock(),
        sleep=no_sleep,
    ).run_once()

    assert processor.answer_counts_at_processing == [1, 1]
    assert gateway.answered_callbacks == [update]
    assert gateway.panels == []
    assert gateway.acknowledgements == []


@pytest.mark.asyncio
async def test_fresh_panel_send_retries_before_offset_advances() -> None:
    update = TelegramUpdate(1, 105, True, 42, "/start")
    gateway = FailsOncePanelGateway(update)

    poller = LocalPoller(
        gateway,
        StaticProcessor(update_result(AcknowledgementKind.PANEL_SHOWN, True)),
        AcquiredPollerLock(),
        sleep=no_sleep,
    )
    await poller.run_once()

    assert gateway.panel_attempts == 2
    assert gateway.panels == [update]
    assert gateway.acknowledgements == []
    assert poller.offset == update.update_id + 1


@pytest.mark.asyncio
async def test_fresh_task_panel_send_retries_before_offset_advances() -> None:
    update = private_callback(109, "tasks:list")
    gateway = FailsOnceTaskPanelGateway(update)
    task_panel = TaskPanelResult(items=(), completion_changed=None)

    poller = LocalPoller(
        gateway,
        StaticProcessor(
            update_result(AcknowledgementKind.TASKS_LISTED, True, task_panel)
        ),
        AcquiredPollerLock(),
        sleep=no_sleep,
    )
    await poller.run_once()

    assert gateway.task_panel_attempts == 2
    assert gateway.task_panels == [(update, task_panel, False)]
    assert poller.offset == update.update_id + 1


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.answered_callback_ids: list[str] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        self.answered_callback_ids.append(callback_query_id)


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_fixed_inline_task_panel_and_answers_callback() -> (
    None
):
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(cast(Bot, bot), bot_id=1)
    panel_update = TelegramUpdate(1, 102, True, 42, "/start")
    callback_update = TelegramUpdate(
        1,
        103,
        True,
        42,
        None,
        callback_query_id="callback-103",
        callback_data="task:await_text",
    )

    await gateway.send_panel(panel_update)
    await gateway.answer_callback(callback_update)

    assert bot.answered_callback_ids == ["callback-103"]
    assert len(bot.sent_messages) == 1
    markup = bot.sent_messages[0]["reply_markup"]
    assert [button.callback_data for button in markup.inline_keyboard[0]] == [
        "tasks:list",
        "search:prompt",
        "memory:ask",
        "projects:list",
    ]
    assert [button.callback_data for button in markup.inline_keyboard[1]] == [
        "capture:note",
        "capture:task",
        "capture:idea",
    ]
    assert [button.callback_data for button in markup.inline_keyboard[2]] == [
        "capture:decision",
        "capture:question",
        "capture:cancel",
    ]
    assert "task:await_text" not in repr(callback_update)


@pytest.mark.asyncio
async def test_aiogram_gateway_sends_numbered_open_tasks_with_completion_buttons() -> (
    None
):
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(cast(Bot, bot), bot_id=1)
    long_title = "x" * 161
    first_id = UUID("00000000-0000-0000-0000-000000000301")
    second_id = UUID("00000000-0000-0000-0000-000000000302")

    await gateway.send_task_panel(
        private_callback(107, "tasks:list"),
        TaskPanelResult(
            items=(
                TaskListItem(first_id, "Купить молоко"),
                TaskListItem(second_id, long_title),
            ),
            completion_changed=None,
        ),
        is_completion=False,
    )

    message = bot.sent_messages[0]
    assert message["text"] == (
        f"📋 Открытые задачи\n\n1. Купить молоко\n2. {'x' * 159}…"
    )
    assert "parse_mode" not in message
    markup = message["reply_markup"]
    assert [row[0].text for row in markup.inline_keyboard] == ["✅ 1", "✅ 2"]
    assert [row[0].callback_data for row in markup.inline_keyboard] == [
        f"tasks:complete:{first_id}",
        f"tasks:complete:{second_id}",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "prefix"),
    [
        (True, "✅ Выполнено."),
        (False, "Задача уже закрыта или недоступна."),
    ],
)
async def test_aiogram_gateway_sends_safe_completion_outcome(
    changed: bool, prefix: str
) -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(cast(Bot, bot), bot_id=1)

    await gateway.send_task_panel(
        private_callback(108, "tasks:complete:any"),
        TaskPanelResult(items=(), completion_changed=changed),
        is_completion=True,
    )

    assert bot.sent_messages[0]["text"] == (f"{prefix}\n\n📋 Открытых задач нет.")
    assert "reply_markup" not in bot.sent_messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "expected_text"),
    [
        ("capture:note", "📝 Заметка"),
        ("capture:task", "✅ Задача"),
        ("capture:idea", "💡 Идея"),
        ("capture:decision", "⚖️ Решение"),
        ("capture:question", "❓ Вопрос"),
        ("capture:cancel", "✖️ Отменено"),
    ],
)
async def test_aiogram_gateway_sends_selection_feedback(
    callback_data: str, expected_text: str
) -> None:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(cast(Bot, bot), bot_id=1)

    await gateway.send_selection_feedback(private_callback(103, callback_data))

    assert bot.sent_messages == [{"chat_id": 42, "text": expected_text}]


class RecordingTaskModePort:
    def __init__(self) -> None:
        self.set_commands: list[SetAwaitingTaskCommand] = []
        self.cancel_commands: list[CancelPendingTaskCommand] = []

    async def set_awaiting_task(
        self, command: SetAwaitingTaskCommand, _transaction: object
    ) -> None:
        self.set_commands.append(command)

    async def cancel(
        self, command: CancelPendingTaskCommand, _transaction: object
    ) -> None:
        self.cancel_commands.append(command)


class RecordingTaskPanelPort:
    def __init__(self) -> None:
        self.list_access: list[AccessContext] = []
        self.complete_commands: list[CompleteTaskCommand] = []
        self.item = TaskListItem(
            id=UUID("00000000-0000-0000-0000-000000000301"),
            title="private task",
        )

    async def list_open(
        self, access_context: AccessContext, _transaction: object
    ) -> TaskPanelResult:
        self.list_access.append(access_context)
        return TaskPanelResult(items=(self.item,), completion_changed=None)

    async def complete(
        self, command: CompleteTaskCommand, _transaction: object
    ) -> TaskPanelResult:
        self.complete_commands.append(command)
        return TaskPanelResult(items=(), completion_changed=True)


def private_callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


@pytest.mark.asyncio
async def test_known_private_tasks_list_returns_transient_task_panel() -> None:
    task_panel_port = RecordingTaskPanelPort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_panel_port=task_panel_port,
    )

    result = await processor.process(private_callback(200, "tasks:list"))

    assert result.kind is AcknowledgementKind.TASKS_LISTED
    assert result.task_panel == TaskPanelResult(
        items=(task_panel_port.item,), completion_changed=None
    )
    assert task_panel_port.list_access == [ACCESS]


@pytest.mark.asyncio
async def test_valid_task_completion_uses_trusted_access_context() -> None:
    task_panel_port = RecordingTaskPanelPort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_panel_port=task_panel_port,
    )

    result = await processor.process(
        private_callback(201, f"tasks:complete:{task_panel_port.item.id}")
    )

    assert result.kind is AcknowledgementKind.TASK_COMPLETED
    assert result.task_panel == TaskPanelResult(items=(), completion_changed=True)
    assert len(task_panel_port.complete_commands) == 1
    command = task_panel_port.complete_commands[0]
    assert command.access_context == ACCESS
    assert command.task_id == task_panel_port.item.id
    assert command.completed_at == NOW


@pytest.mark.asyncio
async def test_malformed_task_completion_returns_safe_own_list() -> None:
    task_panel_port = RecordingTaskPanelPort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_panel_port=task_panel_port,
    )

    result = await processor.process(private_callback(202, "tasks:complete:not-a-uuid"))

    assert result.kind is AcknowledgementKind.TASK_COMPLETED
    assert result.task_panel == TaskPanelResult(
        items=(task_panel_port.item,), completion_changed=False
    )
    assert task_panel_port.complete_commands == []
    assert task_panel_port.list_access == [ACCESS]


@pytest.mark.asyncio
async def test_known_private_task_callback_sets_mode_in_receipt_transaction() -> None:
    task_mode_port = RecordingTaskModePort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_mode_port=task_mode_port,
    )

    result = await processor.process(private_callback(103, "task:await_text"))

    assert result.kind is AcknowledgementKind.TASK_MODE_SET
    assert len(task_mode_port.set_commands) == 1
    assert task_mode_port.set_commands[0].access_context == ACCESS
    assert task_mode_port.cancel_commands == []


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(self, _telegram_user_id: int) -> None:
        return None


@pytest.mark.asyncio
async def test_unknown_or_group_callback_is_ignored_without_mode_change() -> None:
    task_mode_port = RecordingTaskModePort()
    processor = LocalUpdateProcessor(
        UnknownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_mode_port=task_mode_port,
    )
    group_callback = TelegramUpdate(
        1,
        106,
        False,
        42,
        None,
        callback_query_id="callback-106",
        callback_data="task:await_text",
    )

    unknown = await processor.process(private_callback(106, "task:await_text"))
    group = await LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_mode_port=task_mode_port,
    ).process(group_callback)

    assert unknown.kind is AcknowledgementKind.IGNORED
    assert group.kind is AcknowledgementKind.IGNORED
    assert task_mode_port.set_commands == []
    assert task_mode_port.cancel_commands == []


@pytest.mark.asyncio
async def test_known_private_cancel_callback_clears_mode_in_receipt_transaction() -> (
    None
):
    task_mode_port = RecordingTaskModePort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_mode_port=task_mode_port,
    )

    result = await processor.process(private_callback(104, "task:cancel"))

    assert result.kind is AcknowledgementKind.TASK_MODE_CANCELLED
    assert len(task_mode_port.cancel_commands) == 1
    assert task_mode_port.cancel_commands[0].access_context == ACCESS
    assert task_mode_port.set_commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_data", ["task:forged", "", "other:action"])
async def test_malformed_callback_is_ignored_without_mode_change(
    callback_data: str,
) -> None:
    task_mode_port = RecordingTaskModePort()
    processor = LocalUpdateProcessor(
        KnownActorStore(),
        FixedClock(),
        b"test-pepper",
        "test-key",
        task_mode_port=task_mode_port,
    )

    result = await processor.process(private_callback(105, callback_data))

    assert result.kind is AcknowledgementKind.IGNORED
    assert task_mode_port.set_commands == []
    assert task_mode_port.cancel_commands == []

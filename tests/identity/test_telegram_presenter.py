"""Презентер переиспользуем ОТДЕЛЬНО от поллера (seam для webhook).

Прямой вызов ``TelegramPresenter.present`` с фейковым гейтвеем, без
``LocalPoller``: так же его будет звать inbox-шаг воркера на webhook-пути.
Ожидаемые сообщения/клавиатуры не меняются — байт-в-байт закреплено
существующими интеграционными тестами поллера.
"""

from datetime import UTC, datetime

import pytest

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.presenter import TelegramPresenter
from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.tasks.application.contracts import TaskPanelResult

NOW = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)


class FakeGateway:
    """Фейковый гейтвей: только запись вызовов, никакого Telegram."""

    bot_id = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.panel_failures_left = 0

    async def send_panel(self, update: TelegramUpdate) -> None:
        if self.panel_failures_left > 0:
            self.panel_failures_left -= 1
            raise RuntimeError("Telegram send failed")
        self.calls.append(("send_panel", update))

    async def send_task_panel(
        self,
        update: TelegramUpdate,
        result: TaskPanelResult,
        is_completion: bool,
    ) -> None:
        self.calls.append(("send_task_panel", (update, result, is_completion)))

    async def send_reminder_set(self, update: TelegramUpdate, when: datetime) -> None:
        self.calls.append(("send_reminder_set", (update, when)))

    async def send_language_selected(self, update: TelegramUpdate) -> None:
        self.calls.append(("send_language_selected", update))

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append(("send_acknowledgement", kind))


def text_update(update_id: int) -> TelegramUpdate:
    return TelegramUpdate(
        bot_id=1,
        update_id=update_id,
        is_private=True,
        telegram_user_id=42,
        text="заметка",
    )


def update_result(
    kind: AcknowledgementKind, fresh: bool = True, **extra: object
) -> object:
    return type("Result", (), {"kind": kind, "fresh": fresh, **extra})()


@pytest.mark.asyncio
async def test_fresh_panel_result_sends_panel_without_generic_ack() -> None:
    gateway = FakeGateway()
    presenter = TelegramPresenter(gateway)

    await presenter.present(
        text_update(301), update_result(AcknowledgementKind.PANEL_SHOWN)
    )

    assert gateway.calls == [("send_panel", text_update(301))]


@pytest.mark.asyncio
async def test_duplicate_result_sends_nothing() -> None:
    gateway = FakeGateway()
    presenter = TelegramPresenter(gateway)

    await presenter.present(
        text_update(302), update_result(AcknowledgementKind.PANEL_SHOWN, fresh=False)
    )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_fresh_task_completion_sends_task_panel_with_completion_flag() -> None:
    gateway = FakeGateway()
    presenter = TelegramPresenter(gateway)
    task_panel = TaskPanelResult(items=(), completion_changed=True)

    await presenter.present(
        text_update(303),
        update_result(AcknowledgementKind.TASK_COMPLETED, task_panel=task_panel),
    )

    assert gateway.calls == [("send_task_panel", (text_update(303), task_panel, True))]


@pytest.mark.asyncio
async def test_captured_with_reminder_sends_reminder_ack() -> None:
    gateway = FakeGateway()
    presenter = TelegramPresenter(gateway)

    await presenter.present(
        text_update(304),
        update_result(AcknowledgementKind.CAPTURED, reminder_when=NOW),
    )

    assert gateway.calls == [("send_reminder_set", (text_update(304), NOW))]


@pytest.mark.asyncio
async def test_unhandled_kind_falls_back_to_generic_acknowledgement() -> None:
    gateway = FakeGateway()
    presenter = TelegramPresenter(gateway)

    await presenter.present(
        text_update(305), update_result(AcknowledgementKind.ENROLLED)
    )

    assert gateway.calls == [("send_acknowledgement", AcknowledgementKind.ENROLLED)]


@pytest.mark.asyncio
async def test_language_selected_sends_confirmation_then_panel() -> None:
    gateway = FakeGateway()
    presenter = TelegramPresenter(gateway)

    await presenter.present(
        text_update(306), update_result(AcknowledgementKind.LANGUAGE_SELECTED)
    )

    assert [name for name, _payload in gateway.calls] == [
        "send_language_selected",
        "send_panel",
    ]


@pytest.mark.asyncio
async def test_failed_send_is_retried_with_injected_sleep() -> None:
    gateway = FakeGateway()
    gateway.panel_failures_left = 1
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    presenter = TelegramPresenter(gateway, sleep=record_sleep)

    await presenter.present(
        text_update(307), update_result(AcknowledgementKind.PANEL_SHOWN)
    )

    assert gateway.calls == [("send_panel", text_update(307))]
    assert sleeps == [1.0]

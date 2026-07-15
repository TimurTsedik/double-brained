from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import (
    LocalPoller,
    TelegramGateway,
)
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)
from second_brain.slices.projects.application.contracts import (
    ProjectListItem,
    ProjectPanelResult,
)

PROJECT_ID = UUID("00000000-0000-0000-0000-000000000123")
PANEL = ProjectPanelResult(
    items=(ProjectListItem(PROJECT_ID, "Second Brain"),),
    current_project_id=PROJECT_ID,
    action_succeeded=True,
)


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


@pytest.mark.asyncio
async def test_gateway_renders_projects_button_prompt_and_current_panel() -> None:
    bot = RecordingBot()
    gateway = AiogramGateway(cast(Bot, bot), bot_id=1)
    update = TelegramUpdate(1, 1, True, 42, "/start")

    await gateway.send_panel(update)
    await gateway.send_project_name_prompt(update, name_required=False)
    await gateway.send_project_panel(update, PANEL, AcknowledgementKind.PROJECT_CREATED)

    main_markup = bot.messages[0]["reply_markup"]
    assert [button.callback_data for button in main_markup.inline_keyboard[0]] == [
        "tasks:list",
        "search:prompt",
        "memory:ask",
        "projects:list",
    ]
    assert "название" in bot.messages[1]["text"].lower()
    prompt_markup = bot.messages[1]["reply_markup"]
    assert prompt_markup.inline_keyboard[0][0].callback_data == "projects:list"
    assert "Second Brain" in bot.messages[2]["text"]
    assert "создан" not in bot.messages[2]["text"].lower()
    assert "выбран" in bot.messages[2]["text"].lower()
    project_markup = bot.messages[2]["reply_markup"]
    callbacks = [
        button.callback_data for row in project_markup.inline_keyboard for button in row
    ]
    assert f"projects:select:{PROJECT_ID}" in callbacks
    assert "projects:create" in callbacks
    assert "projects:clear" in callbacks


class ProjectGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate, fail_once: bool = False) -> None:
        self.update = update
        self.fail_once = fail_once
        self.attempts = 0
        self.panels: list[tuple[ProjectPanelResult, AcknowledgementKind]] = []

    async def configured_webhook_url(self) -> None:
        return None

    async def get_updates(
        self, _offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        return [self.update]

    async def answer_callback(self, _update: TelegramUpdate) -> None:
        return None

    async def send_project_panel(
        self,
        _update: TelegramUpdate,
        result: ProjectPanelResult,
        kind: AcknowledgementKind,
    ) -> None:
        self.attempts += 1
        if self.fail_once and self.attempts == 1:
            raise RuntimeError("telegram failed")
        self.panels.append((result, kind))


class StaticProcessor:
    def __init__(self, *, fresh: bool) -> None:
        self.fresh = fresh

    async def process(self, _update: TelegramUpdate) -> UpdateResult:
        return UpdateResult(
            kind=AcknowledgementKind.PROJECT_SELECTED,
            trace_id="1" * 32,
            span_id="1" * 16,
            fresh=self.fresh,
            project_panel=PANEL if self.fresh else None,
        )


class Lock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


async def no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_poller_retries_fresh_project_panel_and_skips_duplicate() -> None:
    update = TelegramUpdate(
        1,
        2,
        True,
        42,
        None,
        callback_query_id="callback-2",
        callback_data=f"projects:select:{PROJECT_ID}",
    )
    fresh_gateway = ProjectGateway(update, fail_once=True)
    fresh = LocalPoller(
        cast(TelegramGateway, fresh_gateway),
        StaticProcessor(fresh=True),
        Lock(),
        sleep=no_sleep,
    )

    await fresh.run_once()

    assert fresh_gateway.attempts == 2
    assert fresh_gateway.panels == [(PANEL, AcknowledgementKind.PROJECT_SELECTED)]

    duplicate_gateway = ProjectGateway(update)
    await LocalPoller(
        cast(TelegramGateway, duplicate_gateway),
        StaticProcessor(fresh=False),
        Lock(),
    ).run_once()
    assert duplicate_gateway.panels == []

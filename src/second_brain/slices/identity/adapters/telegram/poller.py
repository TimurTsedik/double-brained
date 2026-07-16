import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)
from second_brain.slices.projects.application.contracts import ProjectPanelResult
from second_brain.slices.retrieval.application.contracts import (
    DigestPage,
    RecordViewResult,
    SearchPanelResult,
)
from second_brain.slices.tasks.application.contracts import TaskPanelResult


class WebhookConfigured(RuntimeError):
    pass


class PollerAlreadyRunning(RuntimeError):
    pass


class TelegramGateway(Protocol):
    bot_id: int

    async def configured_webhook_url(self) -> str | None: ...

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]: ...

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None: ...

    async def send_panel(self, update: TelegramUpdate) -> None: ...

    async def send_invite_link(self, update: TelegramUpdate, link: str) -> None: ...

    async def send_contact_saved(self, update: TelegramUpdate, name: str) -> None: ...

    async def send_selection_feedback(self, update: TelegramUpdate) -> None: ...

    async def send_voice_queued(self, update: TelegramUpdate) -> None: ...

    async def send_reminder_set(
        self, update: TelegramUpdate, when: datetime
    ) -> None: ...

    async def send_task_panel(
        self,
        update: TelegramUpdate,
        result: TaskPanelResult,
        is_completion: bool,
    ) -> None: ...

    async def send_search_prompt(
        self,
        update: TelegramUpdate,
        query_required: bool,
    ) -> None: ...

    async def send_search_cancelled(self, update: TelegramUpdate) -> None: ...

    async def send_memory_prompt(
        self,
        update: TelegramUpdate,
        question_required: bool,
    ) -> None: ...

    async def send_memory_cancelled(self, update: TelegramUpdate) -> None: ...

    async def send_search_panel(
        self,
        update: TelegramUpdate,
        result: SearchPanelResult,
    ) -> None: ...

    async def send_record_view(
        self,
        update: TelegramUpdate,
        result: RecordViewResult,
    ) -> None: ...

    async def send_digest_menu(self, update: TelegramUpdate) -> None: ...

    async def send_digest(
        self,
        update: TelegramUpdate,
        result: DigestPage,
    ) -> None: ...

    async def send_project_name_prompt(
        self,
        update: TelegramUpdate,
        name_required: bool,
    ) -> None: ...

    async def send_project_panel(
        self,
        update: TelegramUpdate,
        result: ProjectPanelResult,
        kind: AcknowledgementKind,
    ) -> None: ...

    async def send_language_prompt(self, update: TelegramUpdate) -> None: ...

    async def send_language_selected(self, update: TelegramUpdate) -> None: ...

    async def answer_callback(self, update: TelegramUpdate) -> None: ...


class UpdateProcessor(Protocol):
    async def process(self, update: TelegramUpdate) -> UpdateResult: ...


class PollerLock(Protocol):
    async def acquire(self, bot_id: int) -> bool: ...


class LocalPoller:
    def __init__(
        self,
        gateway: TelegramGateway,
        processor: UpdateProcessor,
        lock: PollerLock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gateway = gateway
        self._processor = processor
        self._lock = lock
        self._sleep = sleep
        self._started = False
        self.offset: int | None = None

    async def run_once(self) -> None:
        if not self._started:
            if await self._gateway.configured_webhook_url():
                raise WebhookConfigured("local polling refuses a configured webhook")
            bot_id = getattr(self._gateway, "bot_id", None)
            if bot_id is not None and not await self._lock.acquire(bot_id):
                raise PollerAlreadyRunning("another local poller holds this bot lock")
            updates = await self._gateway.get_updates(
                None, ["message", "callback_query"]
            )
            if (
                bot_id is None
                and updates
                and not await self._lock.acquire(updates[0].bot_id)
            ):
                raise PollerAlreadyRunning("another local poller holds this bot lock")
            self._started = True
        else:
            updates = await self._gateway.get_updates(
                self.offset, ["message", "callback_query"]
            )

        for update in updates:
            if update.callback_query_id is not None:
                try:
                    await self._gateway.answer_callback(update)
                except Exception:
                    pass
            while True:
                try:
                    result = await self._processor.process(update)
                except Exception:
                    await self._sleep(1.0)
                    continue
                break
            if result.kind is AcknowledgementKind.PANEL_SHOWN and getattr(
                result, "fresh", True
            ):
                while True:
                    try:
                        await self._gateway.send_panel(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind in {
                AcknowledgementKind.TASK_MODE_SET,
                AcknowledgementKind.TASK_MODE_CANCELLED,
            } and getattr(result, "fresh", True):
                while True:
                    try:
                        await self._gateway.send_selection_feedback(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.VOICE_QUEUED and getattr(
                result, "fresh", True
            ):
                while True:
                    try:
                        await self._gateway.send_voice_queued(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            reminder_when = getattr(result, "reminder_when", None)
            if (
                result.kind is AcknowledgementKind.CAPTURED
                and reminder_when is not None
                and getattr(result, "fresh", True)
            ):
                while True:
                    try:
                        await self._gateway.send_reminder_set(update, reminder_when)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind in {
                AcknowledgementKind.TASKS_LISTED,
                AcknowledgementKind.TASK_COMPLETED,
            } and getattr(result, "fresh", True):
                task_panel = getattr(result, "task_panel", None)
                if task_panel is None:
                    raise RuntimeError("fresh task action did not return a task panel")
                while True:
                    try:
                        await self._gateway.send_task_panel(
                            update,
                            task_panel,
                            result.kind is AcknowledgementKind.TASK_COMPLETED,
                        )
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind in {
                AcknowledgementKind.SEARCH_MODE_SET,
                AcknowledgementKind.SEARCH_QUERY_REQUIRED,
            } and getattr(result, "fresh", True):
                while True:
                    try:
                        await self._gateway.send_search_prompt(
                            update,
                            result.kind is AcknowledgementKind.SEARCH_QUERY_REQUIRED,
                        )
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.SEARCH_MODE_CANCELLED and getattr(
                result, "fresh", True
            ):
                while True:
                    try:
                        await self._gateway.send_search_cancelled(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind in {
                AcknowledgementKind.MEMORY_MODE_SET,
                AcknowledgementKind.MEMORY_QUESTION_REQUIRED,
            } and getattr(result, "fresh", True):
                while True:
                    try:
                        await self._gateway.send_memory_prompt(
                            update,
                            result.kind is AcknowledgementKind.MEMORY_QUESTION_REQUIRED,
                        )
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.MEMORY_MODE_CANCELLED and getattr(
                result, "fresh", True
            ):
                while True:
                    try:
                        await self._gateway.send_memory_cancelled(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.SEARCH_COMPLETED and getattr(
                result, "fresh", True
            ):
                search_panel = getattr(result, "search_panel", None)
                if search_panel is None:
                    raise RuntimeError(
                        "fresh search action did not return a search panel"
                    )
                while True:
                    try:
                        await self._gateway.send_search_panel(update, search_panel)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.RECORD_SHOWN and getattr(
                result, "fresh", True
            ):
                record_view = getattr(result, "record_view", None)
                if record_view is None:
                    raise RuntimeError("fresh record show did not return a record view")
                while True:
                    try:
                        await self._gateway.send_record_view(update, record_view)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.DIGEST_MENU_SHOWN and getattr(
                result, "fresh", True
            ):
                while True:
                    try:
                        await self._gateway.send_digest_menu(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.DIGEST_SHOWN and getattr(
                result, "fresh", True
            ):
                digest_page = getattr(result, "digest_page", None)
                if digest_page is None:
                    raise RuntimeError("fresh digest did not return a page")
                while True:
                    try:
                        await self._gateway.send_digest(update, digest_page)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind in {
                AcknowledgementKind.PROJECT_NAME_MODE_SET,
                AcknowledgementKind.PROJECT_NAME_REQUIRED,
            } and getattr(result, "fresh", True):
                while True:
                    try:
                        await self._gateway.send_project_name_prompt(
                            update,
                            result.kind is AcknowledgementKind.PROJECT_NAME_REQUIRED,
                        )
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind in {
                AcknowledgementKind.PROJECTS_LISTED,
                AcknowledgementKind.PROJECT_CREATED,
                AcknowledgementKind.PROJECT_SELECTED,
                AcknowledgementKind.PROJECT_CLEARED,
            } and getattr(result, "fresh", True):
                project_panel = getattr(result, "project_panel", None)
                if project_panel is None:
                    raise RuntimeError(
                        "fresh project action did not return a project panel"
                    )
                while True:
                    try:
                        await self._gateway.send_project_panel(
                            update, project_panel, result.kind
                        )
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.INVITE_CREATED and getattr(
                result, "fresh", True
            ):
                invite_link = getattr(result, "invite_link", None)
                if invite_link is None:
                    raise RuntimeError("fresh invite creation did not return a link")
                while True:
                    try:
                        await self._gateway.send_invite_link(update, invite_link)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.CONTACT_SAVED and getattr(
                result, "fresh", True
            ):
                contact_name = getattr(result, "contact_name", None)
                if contact_name is None:
                    raise RuntimeError("fresh contact save did not return a name")
                while True:
                    try:
                        await self._gateway.send_contact_saved(update, contact_name)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.LANGUAGE_PROMPT_SHOWN and getattr(
                result, "fresh", True
            ):
                while True:
                    try:
                        await self._gateway.send_language_prompt(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            if result.kind is AcknowledgementKind.LANGUAGE_SELECTED and getattr(
                result, "fresh", True
            ):
                # Two independent retry loops: a transient failure on the panel
                # send must not re-send the "language set" confirmation.
                while True:
                    try:
                        await self._gateway.send_language_selected(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
                while True:
                    try:
                        await self._gateway.send_panel(update)
                    except Exception:
                        await self._sleep(1.0)
                        continue
                    break
            self.offset = update.update_id + 1
            if result.kind not in {
                AcknowledgementKind.IGNORED,
                AcknowledgementKind.CAPTURED,
                AcknowledgementKind.PANEL_SHOWN,
                AcknowledgementKind.TASK_MODE_SET,
                AcknowledgementKind.TASK_MODE_CANCELLED,
                AcknowledgementKind.TASKS_LISTED,
                AcknowledgementKind.TASK_COMPLETED,
                AcknowledgementKind.SEARCH_MODE_SET,
                AcknowledgementKind.SEARCH_MODE_CANCELLED,
                AcknowledgementKind.SEARCH_QUERY_REQUIRED,
                AcknowledgementKind.SEARCH_COMPLETED,
                AcknowledgementKind.RECORD_SHOWN,
                AcknowledgementKind.PROJECTS_LISTED,
                AcknowledgementKind.PROJECT_NAME_MODE_SET,
                AcknowledgementKind.PROJECT_NAME_REQUIRED,
                AcknowledgementKind.PROJECT_CREATED,
                AcknowledgementKind.PROJECT_SELECTED,
                AcknowledgementKind.PROJECT_CLEARED,
                AcknowledgementKind.MEMORY_MODE_SET,
                AcknowledgementKind.MEMORY_MODE_CANCELLED,
                AcknowledgementKind.MEMORY_QUESTION_REQUIRED,
                AcknowledgementKind.LANGUAGE_PROMPT_SHOWN,
                AcknowledgementKind.LANGUAGE_SELECTED,
                AcknowledgementKind.VOICE_QUEUED,
                AcknowledgementKind.INVITE_CREATED,
                AcknowledgementKind.INVITE_FORBIDDEN,
                AcknowledgementKind.CONTACT_SAVED,
                AcknowledgementKind.DIGEST_MENU_SHOWN,
                AcknowledgementKind.DIGEST_SHOWN,
            }:
                try:
                    await self._gateway.send_acknowledgement(update, result.kind)
                except Exception:
                    pass

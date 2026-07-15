from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hmac import digest
from uuid import UUID

from second_brain.shared.clock import Clock
from second_brain.shared.i18n import is_language_chosen
from second_brain.shared.trace import TraceContext
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    CaptureTextPort,
    CaptureVoiceCommand,
    CaptureVoicePort,
)
from second_brain.slices.identity.application.access_context import ResolveAccessContext
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    UpdateStore,
    UpdateTransaction,
)
from second_brain.slices.memory.application.contracts import (
    ConsumeMemoryQuestionCommand,
    MemoryQuestionPort,
    SetAwaitingMemoryCommand,
)
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    CancelProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    ProjectPanelPort,
    ProjectPanelResult,
    SelectProjectCommand,
)
from second_brain.slices.retrieval.application.contracts import (
    ConsumeSearchQueryCommand,
    ExactSearchPort,
    SearchPanelResult,
    SetAwaitingSearchCommand,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    CompleteTaskCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
    TaskModePort,
    TaskPanelPort,
    TaskPanelResult,
)

MAX_ENROLLMENT_ATTEMPTS = 5
ENROLLMENT_ATTEMPT_WINDOW = timedelta(minutes=15)


class AcknowledgementKind(StrEnum):
    CAPTURED = "captured"
    ENROLLED = "enrolled"
    ENROLLMENT_REJECTED = "enrollment_rejected"
    KNOWN_USER_STARTED = "known_user_started"
    PANEL_SHOWN = "panel_shown"
    TASK_MODE_CANCELLED = "task_mode_cancelled"
    TASK_MODE_SET = "task_mode_set"
    TASK_COMPLETED = "task_completed"
    TASKS_LISTED = "tasks_listed"
    SEARCH_COMPLETED = "search_completed"
    SEARCH_MODE_CANCELLED = "search_mode_cancelled"
    SEARCH_MODE_SET = "search_mode_set"
    SEARCH_QUERY_REQUIRED = "search_query_required"
    PROJECTS_LISTED = "projects_listed"
    PROJECT_NAME_MODE_SET = "project_name_mode_set"
    PROJECT_NAME_REQUIRED = "project_name_required"
    PROJECT_CREATED = "project_created"
    PROJECT_SELECTED = "project_selected"
    PROJECT_CLEARED = "project_cleared"
    MEMORY_MODE_SET = "memory_mode_set"
    MEMORY_MODE_CANCELLED = "memory_mode_cancelled"
    MEMORY_QUESTION_QUEUED = "memory_question_queued"
    MEMORY_QUESTION_REQUIRED = "memory_question_required"
    LANGUAGE_PROMPT_SHOWN = "language_prompt_shown"
    LANGUAGE_SELECTED = "language_selected"
    VOICE_QUEUED = "voice_queued"
    IGNORED = "ignored"


@dataclass(frozen=True)
class UpdateResult:
    kind: AcknowledgementKind
    trace_id: str
    span_id: str
    fresh: bool
    task_panel: TaskPanelResult | None = None
    search_panel: SearchPanelResult | None = None
    project_panel: ProjectPanelResult | None = None


@dataclass
class _TransientUpdatePayload:
    task_panel: TaskPanelResult | None = None
    search_panel: SearchPanelResult | None = None
    project_panel: ProjectPanelResult | None = None


class LocalUpdateProcessor:
    def __init__(
        self,
        store: UpdateStore,
        clock: Clock,
        pepper: bytes,
        pepper_key_id: str,
        capture_text_port: CaptureTextPort | None = None,
        task_mode_port: TaskModePort | None = None,
        task_panel_port: TaskPanelPort | None = None,
        exact_search_port: ExactSearchPort | None = None,
        capture_voice_port: CaptureVoicePort | None = None,
        project_panel_port: ProjectPanelPort | None = None,
        memory_ask_port: MemoryQuestionPort | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._pepper = pepper
        self._pepper_key_id = pepper_key_id
        self._capture_text_port = capture_text_port
        self._task_mode_port = task_mode_port
        self._task_panel_port = task_panel_port
        self._exact_search_port = exact_search_port
        self._capture_voice_port = capture_voice_port
        self._project_panel_port = project_panel_port
        self._memory_ask_port = memory_ask_port

    async def process(self, update: TelegramUpdate) -> UpdateResult:
        now = self._clock.now()
        payload = _TransientUpdatePayload()
        receipt = await self._store.process_once(
            update.bot_id,
            update.update_id,
            now,
            lambda transaction: self._process_new(transaction, update, now, payload),
        )
        if receipt.existing:
            context = TraceContext(receipt.trace_id, "1" * 16).new_attempt()
        else:
            if receipt.span_id is None:
                raise RuntimeError("new receipt did not return its span")
            context = TraceContext(receipt.trace_id, receipt.span_id)
        return UpdateResult(
            AcknowledgementKind(receipt.result_kind),
            context.trace_id,
            context.span_id,
            fresh=not receipt.existing,
            task_panel=None if receipt.existing else payload.task_panel,
            search_panel=None if receipt.existing else payload.search_panel,
            project_panel=None if receipt.existing else payload.project_panel,
        )

    async def _process_new(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        now: datetime,
        payload: _TransientUpdatePayload,
    ) -> NewUpdateResult:
        context = TraceContext.new_root()
        kind = await self._process_after_receipt_lock(
            transaction, update, context, now, payload
        )
        return NewUpdateResult(kind, context.trace_id, context.span_id)

    async def _process_after_receipt_lock(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        context: TraceContext,
        now: datetime,
        payload: _TransientUpdatePayload,
    ) -> str:
        if not update.is_private or update.telegram_user_id is None:
            return AcknowledgementKind.IGNORED

        if update.callback_query_id is not None:
            return await self._process_callback(
                transaction, update, context, now, payload
            )

        command, start_token = _parse_start(update.text)
        if command == "start":
            return await self._process_start(
                transaction, update, context, now, start_token
            )
        if _is_command(update.text):
            return AcknowledgementKind.IGNORED

        access_context = await ResolveAccessContext(transaction).execute(
            update.telegram_user_id
        )
        if access_context is None or update.telegram_message_id is None:
            return AcknowledgementKind.IGNORED

        # Forward-only мост: пока язык не выбран (language IS NULL), любое свежее
        # взаимодействие показывает chooser ПЕРЕД действием — действие не
        # выполняется, потому что оно бы ушло дефолтным RU (решение 6 плана).
        language = await transaction.read_user_space_language(access_context)
        if not is_language_chosen(language):
            return AcknowledgementKind.LANGUAGE_PROMPT_SHOWN

        if update.voice is not None:
            if self._exact_search_port is not None:
                await self._exact_search_port.cancel(access_context, transaction)
            if self._memory_ask_port is not None:
                await self._memory_ask_port.cancel(access_context, transaction)
            if self._project_panel_port is not None:
                await self._project_panel_port.cancel_creation(
                    CancelProjectCreationCommand(
                        access_context=access_context,
                        updated_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            if self._capture_voice_port is None:
                raise RuntimeError("capture voice port is required for private voice")
            await self._capture_voice_port.capture(
                CaptureVoiceCommand(
                    access_context=access_context,
                    bot_id=update.bot_id,
                    telegram_update_id=update.update_id,
                    telegram_message_id=update.telegram_message_id,
                    voice=update.voice,
                    received_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.VOICE_QUEUED

        if update.text is None or update.text == "":
            return AcknowledgementKind.IGNORED

        if self._memory_ask_port is not None:
            memory_result = await self._memory_ask_port.consume_question(
                ConsumeMemoryQuestionCommand(
                    access_context=access_context,
                    bot_id=update.bot_id,
                    telegram_update_id=update.update_id,
                    question=update.text,
                    created_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            if memory_result is not None:
                if memory_result.question_required:
                    return AcknowledgementKind.MEMORY_QUESTION_REQUIRED
                return AcknowledgementKind.MEMORY_QUESTION_QUEUED

        if self._project_panel_port is not None:
            project_panel = await self._project_panel_port.consume_name(
                ConsumeProjectNameCommand(
                    access_context=access_context,
                    name=update.text,
                    created_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            if project_panel is not None:
                payload.project_panel = project_panel
                if project_panel.name_required:
                    return AcknowledgementKind.PROJECT_NAME_REQUIRED
                return AcknowledgementKind.PROJECT_CREATED

        if self._exact_search_port is not None:
            search_panel = await self._exact_search_port.consume_query(
                ConsumeSearchQueryCommand(
                    access_context=access_context,
                    query=update.text,
                ),
                transaction,
            )
            if search_panel is not None:
                payload.search_panel = search_panel
                if search_panel.query_required:
                    return AcknowledgementKind.SEARCH_QUERY_REQUIRED
                return AcknowledgementKind.SEARCH_COMPLETED

        if self._capture_text_port is None:
            raise RuntimeError("capture text port is required for private text")
        await self._capture_text_port.capture(
            CaptureTextCommand(
                access_context=access_context,
                bot_id=update.bot_id,
                telegram_update_id=update.update_id,
                telegram_message_id=update.telegram_message_id,
                raw_text=update.text,
                received_at=now,
                trace_id=context.trace_id,
            ),
            transaction,
        )
        return AcknowledgementKind.CAPTURED

    async def _process_callback(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        context: TraceContext,
        now: datetime,
        payload: _TransientUpdatePayload,
    ) -> str:
        selections = {
            "capture:note": "note",
            "capture:task": "task",
            "capture:idea": "idea",
            "capture:decision": "decision",
            "capture:question": "question",
        }
        is_task_completion = (
            update.callback_data is not None
            and update.callback_data.startswith("tasks:complete:")
        )
        is_project_selection = (
            update.callback_data is not None
            and update.callback_data.startswith("projects:select:")
        )
        if (
            update.callback_data
            not in {
                *selections,
                "capture:cancel",
                "task:await_text",
                "task:cancel",
                "tasks:list",
                "search:prompt",
                "search:cancel",
                "memory:ask",
                "memory:cancel",
                "projects:list",
                "projects:create",
                "projects:clear",
                "lang:menu",
                "lang:ru",
                "lang:en",
            }
            and not is_task_completion
            and not is_project_selection
        ):
            return AcknowledgementKind.IGNORED
        if update.telegram_user_id is None:
            return AcknowledgementKind.IGNORED
        access_context = await ResolveAccessContext(transaction).execute(
            update.telegram_user_id
        )
        if access_context is None:
            return AcknowledgementKind.IGNORED
        # lang:* — единственное исключение из forward-only гейта: выбор языка
        # обязан пройти даже при language IS NULL, иначе его нельзя применить.
        is_language_callback = (
            update.callback_data is not None
            and update.callback_data.startswith("lang:")
        )
        if not is_language_callback:
            language = await transaction.read_user_space_language(access_context)
            if not is_language_chosen(language):
                return AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
        if is_language_callback:
            return await self._process_language_callback(
                transaction, update, access_context, context, now
            )
        # Any panel button other than "Ask memory" clears the one-shot memory
        # mode, so a queued question never sticks onto an unrelated next text.
        if self._memory_ask_port is not None and update.callback_data != "memory:ask":
            await self._memory_ask_port.cancel(access_context, transaction)
        if update.callback_data == "memory:ask":
            if self._memory_ask_port is None:
                return AcknowledgementKind.IGNORED
            if self._task_mode_port is not None:
                await self._task_mode_port.cancel(
                    CancelPendingTaskCommand(
                        access_context=access_context,
                        updated_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            if self._exact_search_port is not None:
                await self._exact_search_port.cancel(access_context, transaction)
            if self._project_panel_port is not None:
                await self._project_panel_port.cancel_creation(
                    CancelProjectCreationCommand(
                        access_context=access_context,
                        updated_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            await self._memory_ask_port.set_awaiting(
                SetAwaitingMemoryCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.MEMORY_MODE_SET
        if update.callback_data == "memory:cancel":
            if self._memory_ask_port is None:
                return AcknowledgementKind.IGNORED
            return AcknowledgementKind.MEMORY_MODE_CANCELLED
        if update.callback_data == "projects:list":
            if self._project_panel_port is None:
                return AcknowledgementKind.IGNORED
            await self._project_panel_port.cancel_creation(
                CancelProjectCreationCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            payload.project_panel = await self._project_panel_port.list_projects(
                access_context, transaction
            )
            return AcknowledgementKind.PROJECTS_LISTED
        if update.callback_data == "projects:create":
            if self._project_panel_port is None:
                return AcknowledgementKind.IGNORED
            if self._task_mode_port is not None:
                await self._task_mode_port.cancel(
                    CancelPendingTaskCommand(
                        access_context=access_context,
                        updated_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            if self._exact_search_port is not None:
                await self._exact_search_port.cancel(access_context, transaction)
            await self._project_panel_port.begin_creation(
                BeginProjectCreationCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.PROJECT_NAME_MODE_SET
        if update.callback_data == "projects:clear":
            if self._project_panel_port is None:
                return AcknowledgementKind.IGNORED
            payload.project_panel = await self._project_panel_port.clear(
                ClearCurrentProjectCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.PROJECT_CLEARED
        if is_project_selection:
            if self._project_panel_port is None or update.callback_data is None:
                return AcknowledgementKind.IGNORED
            await self._project_panel_port.cancel_creation(
                CancelProjectCreationCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            raw_project_id = update.callback_data.removeprefix("projects:select:")
            try:
                project_id = UUID(raw_project_id)
            except ValueError:
                listed_projects = await self._project_panel_port.list_projects(
                    access_context, transaction
                )
                payload.project_panel = ProjectPanelResult(
                    items=listed_projects.items,
                    current_project_id=listed_projects.current_project_id,
                    action_succeeded=False,
                )
            else:
                payload.project_panel = await self._project_panel_port.select(
                    SelectProjectCommand(
                        access_context=access_context,
                        project_id=project_id,
                        updated_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            return AcknowledgementKind.PROJECT_SELECTED
        if update.callback_data == "search:prompt":
            if self._exact_search_port is None or self._task_mode_port is None:
                return AcknowledgementKind.IGNORED
            await self._task_mode_port.cancel(
                CancelPendingTaskCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            if self._project_panel_port is not None:
                await self._project_panel_port.cancel_creation(
                    CancelProjectCreationCommand(
                        access_context=access_context,
                        updated_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            await self._exact_search_port.set_awaiting(
                SetAwaitingSearchCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.SEARCH_MODE_SET
        if update.callback_data == "search:cancel":
            if self._exact_search_port is None:
                return AcknowledgementKind.IGNORED
            await self._exact_search_port.cancel(access_context, transaction)
            return AcknowledgementKind.SEARCH_MODE_CANCELLED
        if update.callback_data == "tasks:list":
            if self._task_panel_port is None:
                return AcknowledgementKind.IGNORED
            payload.task_panel = await self._task_panel_port.list_open(
                access_context, transaction
            )
            return AcknowledgementKind.TASKS_LISTED
        if is_task_completion:
            if self._task_panel_port is None or update.callback_data is None:
                return AcknowledgementKind.IGNORED
            raw_task_id = update.callback_data.removeprefix("tasks:complete:")
            try:
                task_id = UUID(raw_task_id)
            except ValueError:
                listed_tasks = await self._task_panel_port.list_open(
                    access_context, transaction
                )
                payload.task_panel = TaskPanelResult(
                    items=listed_tasks.items,
                    completion_changed=False,
                )
            else:
                payload.task_panel = await self._task_panel_port.complete(
                    CompleteTaskCommand(
                        access_context=access_context,
                        task_id=task_id,
                        completed_at=now,
                        trace_id=context.trace_id,
                    ),
                    transaction,
                )
            return AcknowledgementKind.TASK_COMPLETED
        if self._task_mode_port is None:
            return AcknowledgementKind.IGNORED
        if self._exact_search_port is not None:
            await self._exact_search_port.cancel(access_context, transaction)
        if self._project_panel_port is not None:
            await self._project_panel_port.cancel_creation(
                CancelProjectCreationCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
        if update.callback_data == "task:await_text":
            await self._task_mode_port.set_awaiting_task(
                SetAwaitingTaskCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.TASK_MODE_SET
        if update.callback_data in selections:
            selection = selections[update.callback_data]
            await self._task_mode_port.set_selection(
                SetPendingCaptureSelectionCommand(
                    access_context=access_context,
                    selection=selection,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
            return AcknowledgementKind.TASK_MODE_SET
        await self._task_mode_port.cancel(
            CancelPendingTaskCommand(
                access_context=access_context,
                updated_at=now,
                trace_id=context.trace_id,
            ),
            transaction,
        )
        return AcknowledgementKind.TASK_MODE_CANCELLED

    async def _process_language_callback(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        access_context: AccessContext,
        context: TraceContext,
        now: datetime,
    ) -> str:
        # Смена языка не должна «залипать» на прежних awaiting-режимах.
        await self._cancel_awaiting_modes(transaction, access_context, context, now)
        if update.callback_data == "lang:menu":
            return AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
        if update.callback_data == "lang:ru":
            await transaction.set_user_space_language(access_context, "ru", now)
            return AcknowledgementKind.LANGUAGE_SELECTED
        if update.callback_data == "lang:en":
            await transaction.set_user_space_language(access_context, "en", now)
            return AcknowledgementKind.LANGUAGE_SELECTED
        return AcknowledgementKind.IGNORED

    async def _cancel_awaiting_modes(
        self,
        transaction: UpdateTransaction,
        access_context: AccessContext,
        context: TraceContext,
        now: datetime,
    ) -> None:
        if self._task_mode_port is not None:
            await self._task_mode_port.cancel(
                CancelPendingTaskCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
        if self._exact_search_port is not None:
            await self._exact_search_port.cancel(access_context, transaction)
        if self._project_panel_port is not None:
            await self._project_panel_port.cancel_creation(
                CancelProjectCreationCommand(
                    access_context=access_context,
                    updated_at=now,
                    trace_id=context.trace_id,
                ),
                transaction,
            )
        if self._memory_ask_port is not None:
            await self._memory_ask_port.cancel(access_context, transaction)

    async def _process_start(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        context: TraceContext,
        now: datetime,
        start_token: str | None,
    ) -> str:
        if update.telegram_user_id is None:
            return AcknowledgementKind.IGNORED

        access_context = await ResolveAccessContext(transaction).execute(
            update.telegram_user_id
        )
        if access_context is not None:
            if start_token is not None:
                return AcknowledgementKind.IGNORED
            language = await transaction.read_user_space_language(access_context)
            if not is_language_chosen(language):
                return AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
            return AcknowledgementKind.PANEL_SHOWN

        actor_digest = self._actor_digest(update.bot_id, update.telegram_user_id)
        attempt = await transaction.reserve_enrollment_attempt(
            update.bot_id,
            actor_digest,
            self._pepper_key_id,
            context.trace_id,
            now,
        )
        if not attempt.admitted:
            return AcknowledgementKind.ENROLLMENT_REJECTED
        if start_token is None:
            await transaction.finish_enrollment_attempt(attempt.id, "missing_token")
            return AcknowledgementKind.ENROLLMENT_REJECTED

        outcome = await transaction.enroll_telegram_user(
            digest(self._pepper, start_token.encode(), "sha256"),
            self._pepper_key_id,
            update.telegram_user_id,
            now,
        )
        kind = (
            AcknowledgementKind.ENROLLED
            if outcome.value == AcknowledgementKind.ENROLLED
            else AcknowledgementKind.ENROLLMENT_REJECTED
        )
        await transaction.finish_enrollment_attempt(attempt.id, kind.value)
        # Сразу после ENROLLED показываем выбор языка, а не «Enrollment complete.»
        # (решение 6): новый юзер выбирает язык до первой панели.
        if kind is AcknowledgementKind.ENROLLED:
            return AcknowledgementKind.LANGUAGE_PROMPT_SHOWN
        return kind

    def _actor_digest(self, bot_id: int, telegram_user_id: int) -> bytes:
        actor = f"{bot_id}:{telegram_user_id}".encode()
        return digest(self._pepper, b"enrollment-attempt-actor-v1:" + actor, "sha256")


def _parse_start(text: str | None) -> tuple[str | None, str | None]:
    if text is None:
        return None, None
    command, _, token = text.partition(" ")
    if command != "/start":
        return None, None
    return "start", token.strip() or None


def _is_command(text: str | None) -> bool:
    return text is not None and text.lstrip().startswith("/")

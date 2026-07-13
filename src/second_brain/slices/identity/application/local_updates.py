from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hmac import digest

from second_brain.shared.clock import Clock
from second_brain.shared.trace import TraceContext
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    CaptureTextPort,
)
from second_brain.slices.identity.application.access_context import ResolveAccessContext
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    UpdateStore,
    UpdateTransaction,
)
from second_brain.slices.tasks.application.contracts import (
    CancelPendingTaskCommand,
    SetAwaitingTaskCommand,
    SetPendingCaptureSelectionCommand,
    TaskModePort,
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
    IGNORED = "ignored"


@dataclass(frozen=True)
class UpdateResult:
    kind: AcknowledgementKind
    trace_id: str
    span_id: str
    fresh: bool


class LocalUpdateProcessor:
    def __init__(
        self,
        store: UpdateStore,
        clock: Clock,
        pepper: bytes,
        pepper_key_id: str,
        capture_text_port: CaptureTextPort | None = None,
        task_mode_port: TaskModePort | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._pepper = pepper
        self._pepper_key_id = pepper_key_id
        self._capture_text_port = capture_text_port
        self._task_mode_port = task_mode_port

    async def process(self, update: TelegramUpdate) -> UpdateResult:
        now = self._clock.now()
        receipt = await self._store.process_once(
            update.bot_id,
            update.update_id,
            now,
            lambda transaction: self._process_new(transaction, update, now),
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
        )

    async def _process_new(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        now: datetime,
    ) -> NewUpdateResult:
        context = TraceContext.new_root()
        kind = await self._process_after_receipt_lock(transaction, update, context, now)
        return NewUpdateResult(kind, context.trace_id, context.span_id)

    async def _process_after_receipt_lock(
        self,
        transaction: UpdateTransaction,
        update: TelegramUpdate,
        context: TraceContext,
        now: datetime,
    ) -> str:
        if not update.is_private or update.telegram_user_id is None:
            return AcknowledgementKind.IGNORED

        if update.callback_query_id is not None:
            return await self._process_callback(transaction, update, context, now)

        command, start_token = _parse_start(update.text)
        if command == "start":
            return await self._process_start(
                transaction, update, context, now, start_token
            )
        if _is_command(update.text) or update.text is None or update.text == "":
            return AcknowledgementKind.IGNORED

        access_context = await ResolveAccessContext(transaction).execute(
            update.telegram_user_id
        )
        if access_context is None or update.telegram_message_id is None:
            return AcknowledgementKind.IGNORED

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
    ) -> str:
        selections = {
            "capture:note": "note",
            "capture:task": "task",
            "capture:idea": "idea",
            "capture:decision": "decision",
            "capture:question": "question",
        }
        if update.callback_data not in {
            *selections,
            "capture:cancel",
            "task:await_text",
            "task:cancel",
        }:
            return AcknowledgementKind.IGNORED
        if update.telegram_user_id is None:
            return AcknowledgementKind.IGNORED
        access_context = await ResolveAccessContext(transaction).execute(
            update.telegram_user_id
        )
        if access_context is None or self._task_mode_port is None:
            return AcknowledgementKind.IGNORED
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
            if start_token is None:
                return AcknowledgementKind.PANEL_SHOWN
            return AcknowledgementKind.IGNORED

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

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.shared.i18n import Locale
from second_brain.slices.identity.adapters.telegram.gateway import (
    SHOW_BUTTONS_PER_ROW,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    TelegramRecipient,
    WorkerIdentityPort,
)
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryWriter,
)
from second_brain.slices.memory.application.contracts import (
    AnswerDeliveryPort,
    AnswerSourceRef,
    DeliveryPayload,
)
from second_brain.slices.memory.application.render import (
    render_answer,
    render_safe_failure,
    render_source_label,
)
from second_brain.slices.memory.domain.entities import (
    EvidenceLevel,
    MemoryAnswer,
    MemoryRunStatus,
)
from second_brain.slices.memory.ports.repositories import SucceedMemoryStepCommand

# Any terminal-but-unanswered upstream (reasoning FAILED, or retrieval FAILED so
# reasoning never ran) delivers the same honest failure code.
DELIVERY_FAILURE_CODE = "memory_answer_unavailable"


@dataclass(frozen=True)
class CompleteMemoryDeliveryCommand:
    access_context: AccessContext = field(repr=False)
    step_id: UUID = field(repr=False)
    run_id: UUID = field(repr=False)
    trace_id: str = field(repr=False)
    completed_at: datetime


class AiogramAnswerDelivery:
    """AnswerDeliveryPort over aiogram. Sends the answer as PLAIN TEXT with no
    parse_mode, mirroring AiogramVoiceNotifier: model output must never be
    interpreted as Markdown/HTML markup. Rendering (in the user's locale) already
    happened in the completion, which has the language; this adapter has only a
    TelegramRecipient, so it never renders and just forwards payload.text.

    When the payload carries source refs, the message gets numbered show-buttons
    (same audited show:<kind>:<uuid> callbacks as search results; the polling
    process answers them even though the worker sent the message). No sources —
    no reply_markup, message unchanged."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def deliver(
        self, payload: DeliveryPayload, recipient_context: TelegramRecipient
    ) -> None:
        if payload.sources:
            await self._bot.send_message(
                recipient_context.telegram_user_id,
                payload.text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=_source_button_rows(payload.sources)
                ),
            )
            return
        await self._bot.send_message(recipient_context.telegram_user_id, payload.text)


def _source_button_rows(
    sources: tuple[AnswerSourceRef, ...],
) -> list[list[InlineKeyboardButton]]:
    # Номерные кнопки «1…N» в рядах по 5, как у результатов поиска: та же
    # ширина ряда (SHOW_BUTTONS_PER_ROW) и тот же строгий callback show:тип:uuid.
    buttons = [
        InlineKeyboardButton(
            text=f"{number}",
            callback_data=f"show:{ref.record_kind.value}:{ref.record_id}",
        )
        for number, ref in enumerate(sources, start=1)
    ]
    return [
        buttons[index : index + SHOW_BUTTONS_PER_ROW]
        for index in range(0, len(buttons), SHOW_BUTTONS_PER_ROW)
    ]


class MemoryDeliveryCompletionInTransaction:
    """Delivery does NOT cascade-skip when reasoning failed. It reads the
    terminal reasoning state: SUCCEEDED with an answer -> render_answer success
    payload; otherwise (reasoning FAILED, or retrieval FAILED leaving reasoning
    unreachable) -> a safe-failure payload carrying only a code and the trace
    reference. Either way the user hears back, then the delivery step succeeds."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        delivery_port: AnswerDeliveryPort,
        identity: WorkerIdentityPort,
    ) -> None:
        self._session_factory = session_factory
        self._delivery_port = delivery_port
        self._identity = identity

    async def complete(self, command: CompleteMemoryDeliveryCommand) -> None:
        async with self._session_factory() as session, session.begin():
            writer = PostgresMemoryWriter(session)
            payload = await self._build_payload(writer, command)
            recipient = await self._identity.resolve_telegram_recipient(
                command.access_context
            )
            await self._delivery_port.deliver(payload, recipient)
            await writer.succeed_step(
                SucceedMemoryStepCommand(
                    access_context=command.access_context,
                    step_id=command.step_id,
                    completed_at=command.completed_at,
                )
            )

    async def _build_payload(
        self,
        writer: PostgresMemoryWriter,
        command: CompleteMemoryDeliveryCommand,
    ) -> DeliveryPayload:
        # Resolve the user's locale here (decision 5: one narrow read at message
        # build time) and render both the success and failure chrome in it, so
        # the adapter — which only holds a TelegramRecipient — never renders.
        locale = await self._identity.resolve_locale(command.access_context)
        state = await writer.read_reasoning_state(
            command.access_context, command.run_id
        )
        if state is not None and state.status is MemoryRunStatus.SUCCEEDED:
            answer = await writer.read_answer(command.access_context, command.run_id)
            if answer is not None:
                return DeliveryPayload.success(
                    render_answer(answer, locale),
                    sources=_source_refs(answer, locale),
                )
        return DeliveryPayload(
            text=render_safe_failure(command.trace_id, locale),
            safe_error_code=DELIVERY_FAILURE_CODE,
            trace_id=command.trace_id,
        )


def _source_refs(answer: MemoryAnswer, locale: Locale) -> tuple[AnswerSourceRef, ...]:
    # Mirrors render_answer: an ∅/insufficient answer shows no "Источники:"
    # lines, so it also gets no source refs (and thus no buttons).
    if answer.evidence_level is EvidenceLevel.INSUFFICIENT:
        return ()
    return tuple(
        AnswerSourceRef(
            record_kind=source.record_kind,
            label=render_source_label(source, locale),
            record_id=source.record_id,
        )
        for source in answer.sources
    )

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    DeliveryPayload,
)
from second_brain.slices.memory.application.render import (
    render_answer,
    render_safe_failure,
)
from second_brain.slices.memory.domain.entities import MemoryRunStatus
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
    interpreted as Markdown/HTML markup. Rendering lives in the application
    (render_answer / render_safe_failure); this adapter only sends the string."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def deliver(
        self, payload: DeliveryPayload, recipient_context: TelegramRecipient
    ) -> None:
        if payload.text is not None:
            text = payload.text
        else:
            text = render_safe_failure(payload.trace_id or "")
        await self._bot.send_message(recipient_context.telegram_user_id, text)


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
        state = await writer.read_reasoning_state(
            command.access_context, command.run_id
        )
        if state is not None and state.status is MemoryRunStatus.SUCCEEDED:
            answer = await writer.read_answer(command.access_context, command.run_id)
            if answer is not None:
                return DeliveryPayload.success(render_answer(answer))
        return DeliveryPayload.failure(DELIVERY_FAILURE_CODE, command.trace_id)

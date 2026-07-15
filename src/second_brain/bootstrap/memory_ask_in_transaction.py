from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryWriter,
)
from second_brain.slices.memory.application.contracts import (
    ConsumeMemoryQuestionCommand,
    MemoryAskResult,
    MemoryQuestionPort,
    SetAwaitingMemoryCommand,
)
from second_brain.slices.memory.ports.repositories import CreateMemoryQuestionCommand


class MemoryAskInTransaction(MemoryQuestionPort):
    """Bootstrap composition for the one-shot Ask-memory mode.

    The whole set/consume/cancel dance runs inside the caller's ingress
    transaction: ``lock_pending`` takes ``SELECT ... FOR UPDATE`` on the mode
    row, so two quick texts after "Ask memory" serialise on that lock and only
    one of them creates the question + run.
    """

    async def set_awaiting(
        self,
        command: SetAwaitingMemoryCommand,
        transaction: UpdateTransaction,
    ) -> None:
        await _writer(transaction).set_awaiting(command)

    async def cancel(
        self,
        access_context: AccessContext,
        transaction: UpdateTransaction,
    ) -> None:
        await _writer(transaction).cancel(access_context)

    async def consume_question(
        self,
        command: ConsumeMemoryQuestionCommand,
        transaction: UpdateTransaction,
    ) -> MemoryAskResult | None:
        writer = _writer(transaction)
        if not await writer.lock_pending(command.access_context):
            return None
        question = " ".join(command.question.split())
        if not question:
            return MemoryAskResult(question_required=True)
        await writer.create_question(
            CreateMemoryQuestionCommand(
                access_context=command.access_context,
                bot_id=command.bot_id,
                telegram_update_id=command.telegram_update_id,
                question_text=question,
                current_project_id=command.current_project_id,
                created_at=command.created_at,
                trace_id=command.trace_id,
            )
        )
        await writer.cancel(command.access_context)
        return MemoryAskResult(question_required=False)


def _writer(transaction: UpdateTransaction) -> PostgresMemoryWriter:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("memory ask requires the PostgreSQL update transaction")
    return PostgresMemoryWriter(transaction.active_session)

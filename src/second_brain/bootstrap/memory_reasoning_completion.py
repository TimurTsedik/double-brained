from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.adapters.persistence.repository import (
    PostgresMemoryWriter,
)
from second_brain.slices.memory.application.answer_question import AnswerMemoryQuestion
from second_brain.slices.memory.ports.repositories import (
    SaveMemoryAnswerCommand,
    SucceedMemoryStepCommand,
)


@dataclass(frozen=True)
class CompleteMemoryReasoningCommand:
    access_context: AccessContext = field(repr=False)
    step_id: UUID = field(repr=False)
    run_id: UUID = field(repr=False)
    completed_at: datetime


class MemoryReasoningCompletionInTransaction:
    """Reasons over the durable evidence snapshot (never the live index) so a
    retry reads the same evidence and produces the same answer. Empty snapshot
    -> insufficient answer without touching the provider. The answer is saved
    idempotently by run_id, then the reasoning step succeeds — one transaction.
    A provider or contract failure propagates so the worker fails this step."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        answerer: AnswerMemoryQuestion,
    ) -> None:
        self._session_factory = session_factory
        self._answerer = answerer

    async def complete(self, command: CompleteMemoryReasoningCommand) -> None:
        async with self._session_factory() as session, session.begin():
            writer = PostgresMemoryWriter(session)
            question = await writer.read_run_question(
                command.access_context, command.run_id
            )
            if question is None:
                raise LookupError("memory run question was not found")
            snapshot = await writer.read_evidence_snapshot(
                command.access_context, command.run_id
            )
            answer = await self._answerer.execute(question.question_text, snapshot)
            await writer.save_answer(
                SaveMemoryAnswerCommand(
                    access_context=command.access_context,
                    run_id=command.run_id,
                    answer=answer,
                    created_at=command.completed_at,
                    trace_id=question.trace_id,
                )
            )
            await writer.succeed_step(
                SucceedMemoryStepCommand(
                    access_context=command.access_context,
                    step_id=command.step_id,
                    completed_at=command.completed_at,
                )
            )

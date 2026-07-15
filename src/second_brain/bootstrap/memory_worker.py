import re
from datetime import datetime, timedelta
from typing import Protocol

from second_brain.bootstrap.memory_delivery import CompleteMemoryDeliveryCommand
from second_brain.bootstrap.memory_reasoning_completion import (
    CompleteMemoryReasoningCommand,
)
from second_brain.bootstrap.memory_retrieval_completion import (
    CompleteMemoryRetrievalCommand,
)
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.memory.domain.entities import (
    MemoryAnswerStep,
    MemoryRunClaim,
    MemoryStepType,
)
from second_brain.slices.memory.ports.repositories import FailMemoryStepCommand

DEFAULT_STEP_LEASE = timedelta(minutes=15)
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


class MemoryRunQueue(Protocol):
    async def claim_due_run(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
    ) -> MemoryRunClaim | None: ...

    async def fail_step(self, command: FailMemoryStepCommand) -> MemoryAnswerStep: ...


class RetrievalCompletion(Protocol):
    async def complete(self, command: CompleteMemoryRetrievalCommand) -> None: ...


class ReasoningCompletion(Protocol):
    async def complete(self, command: CompleteMemoryReasoningCommand) -> None: ...


class DeliveryCompletion(Protocol):
    async def complete(self, command: CompleteMemoryDeliveryCommand) -> None: ...


class MemoryWorker:
    """Claims exactly one lowest due step per cycle and finishes it strictly on
    its own RUNNING row. Each of retrieval/reasoning/delivery is a separate
    claim + completion so a failure lands on the right step and the bounded
    attempt budget never leaks across steps. On error the worker fails its own
    step with a safe code (default memory_answer_failed)."""

    def __init__(
        self,
        *,
        queue: MemoryRunQueue,
        retrieval: RetrievalCompletion,
        reasoning: ReasoningCompletion,
        delivery: DeliveryCompletion,
        step_lease: timedelta = DEFAULT_STEP_LEASE,
    ) -> None:
        if step_lease <= timedelta(0):
            raise ValueError("memory step lease must be positive")
        self._queue = queue
        self._retrieval = retrieval
        self._reasoning = reasoning
        self._delivery = delivery
        self._step_lease = step_lease

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        claim = await self._queue.claim_due_run(access_context, now, self._step_lease)
        if claim is None:
            return False
        try:
            await self._dispatch(access_context, claim, now)
        except Exception as error:
            await self._queue.fail_step(
                FailMemoryStepCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    failed_at=now,
                    safe_error_code=_safe_error_code(error),
                )
            )
        return True

    async def _dispatch(
        self,
        access_context: AccessContext,
        claim: MemoryRunClaim,
        now: datetime,
    ) -> None:
        if claim.step_type is MemoryStepType.RETRIEVAL:
            await self._retrieval.complete(
                CompleteMemoryRetrievalCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    run_id=claim.run_id,
                    completed_at=now,
                )
            )
        elif claim.step_type is MemoryStepType.REASONING:
            await self._reasoning.complete(
                CompleteMemoryReasoningCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    run_id=claim.run_id,
                    completed_at=now,
                )
            )
        else:
            await self._delivery.complete(
                CompleteMemoryDeliveryCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    run_id=claim.run_id,
                    trace_id=claim.trace_id,
                    completed_at=now,
                )
            )


def _safe_error_code(error: Exception) -> str:
    value = getattr(error, "safe_error_code", None)
    if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value):
        return value
    return "memory_answer_failed"

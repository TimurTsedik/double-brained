import re
from datetime import datetime, timedelta
from typing import Protocol

from second_brain.bootstrap.indexing_completion import CompleteIndexingCommand
from second_brain.bootstrap.indexing_source import ReadIndexingSourceCommand
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    FailProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import ProcessingStepType
from second_brain.slices.processing.ports.repositories import ProcessingQueue
from second_brain.slices.retrieval.application.contracts import IndexingSource
from second_brain.slices.retrieval.application.indexing import IndexSource

DEFAULT_STEP_LEASE = timedelta(minutes=15)
INDEXING_STEP_TYPES = (ProcessingStepType.INDEXING,)
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


class IndexingSourcePort(Protocol):
    async def read(self, command: ReadIndexingSourceCommand) -> IndexingSource: ...


class IndexingCompletionPort(Protocol):
    async def complete(self, command: CompleteIndexingCommand) -> None: ...


class IndexingWorker:
    """Mirrors ClassificationWorker for the INDEXING step: claim, read the
    registered target's text, embed, complete; failures become safe codes."""

    def __init__(
        self,
        *,
        queue: ProcessingQueue,
        source_reader: IndexingSourcePort,
        indexer: IndexSource,
        completion: IndexingCompletionPort,
        step_lease: timedelta = DEFAULT_STEP_LEASE,
    ) -> None:
        if step_lease <= timedelta(0):
            raise ValueError("indexing step lease must be positive")
        self._queue = queue
        self._source_reader = source_reader
        self._indexer = indexer
        self._completion = completion
        self._step_lease = step_lease

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        claim = await self._queue.claim_due_step(
            access_context,
            now,
            self._step_lease,
            INDEXING_STEP_TYPES,
        )
        if claim is None:
            return False
        try:
            source = await self._source_reader.read(
                ReadIndexingSourceCommand(
                    access_context=access_context,
                    processing_run_id=claim.run_id,
                )
            )
            outcome = await self._indexer.execute(source)
            await self._completion.complete(
                CompleteIndexingCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    outcome=outcome,
                    completed_at=now,
                )
            )
        except Exception as error:
            await self._queue.fail_step(
                FailProcessingStepCommand(
                    access_context=access_context,
                    step_id=claim.step_id,
                    failed_at=now,
                    safe_error_code=_safe_error_code(error),
                )
            )
        return True


def _safe_error_code(error: Exception) -> str:
    value = getattr(error, "safe_error_code", None)
    if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value):
        return value
    return "indexing_failed"

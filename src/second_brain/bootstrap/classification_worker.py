import re
from datetime import datetime, timedelta

from second_brain.slices.classification.application.contracts import (
    ClassificationCompletionPort,
    ClassificationSourcePort,
    CompleteClassificationCommand,
    ReadClassificationSourceCommand,
)
from second_brain.slices.classification.application.extraction import ClassifySource
from second_brain.slices.classification.domain.entities import CandidateType
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    FailProcessingStepCommand,
    SkipProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import ProcessingStepType
from second_brain.slices.processing.ports.repositories import ProcessingQueue

DEFAULT_STEP_LEASE = timedelta(minutes=15)
CLASSIFICATION_STEP_TYPES = (ProcessingStepType.CLASSIFICATION,)
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


class ClassificationWorker:
    def __init__(
        self,
        *,
        queue: ProcessingQueue,
        source_reader: ClassificationSourcePort,
        classifier: ClassifySource,
        completion: ClassificationCompletionPort,
        step_lease: timedelta = DEFAULT_STEP_LEASE,
    ) -> None:
        if step_lease <= timedelta(0):
            raise ValueError("classification step lease must be positive")
        self._queue = queue
        self._source_reader = source_reader
        self._classifier = classifier
        self._completion = completion
        self._step_lease = step_lease

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        claim = await self._queue.claim_due_step(
            access_context,
            now,
            self._step_lease,
            CLASSIFICATION_STEP_TYPES,
        )
        if claim is None:
            return False
        try:
            # Source-only прогоны (output_type NULL) шага CLASSIFICATION не
            # имеют — NULL здесь означает порчу данных, честно валим шаг.
            if claim.output_type is None:
                raise ValueError("classification requires a typed processing run")
            source = await self._source_reader.read(
                ReadClassificationSourceCommand(
                    access_context=access_context,
                    processing_run_id=claim.run_id,
                    capture_event_id=claim.capture_event_id,
                    base_type=CandidateType(claim.output_type.value),
                )
            )
            outcome = await self._classifier.execute(source)
            if outcome.skipped_reason is not None:
                await self._queue.skip_step(
                    SkipProcessingStepCommand(
                        access_context=access_context,
                        step_id=claim.step_id,
                        skipped_at=now,
                        safe_reason_code=outcome.skipped_reason,
                    )
                )
            else:
                await self._completion.complete(
                    CompleteClassificationCommand(
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
    return "classification_failed"

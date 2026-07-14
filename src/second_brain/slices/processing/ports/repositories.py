from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingRun,
    ProcessingStep,
    ProcessingStepClaim,
)


class ProcessingRepository(Protocol):
    async def create_voice_run(
        self, command: CreateVoiceProcessingRunCommand
    ) -> ProcessingRun: ...

    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProcessingStepClaim | None: ...

    async def succeed_step(
        self, command: SucceedProcessingStepCommand
    ) -> ProcessingStep: ...

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep: ...

    async def get_run(
        self, access_context: AccessContext, run_id: UUID
    ) -> ProcessingRun | None: ...

    async def count_runs(self, access_context: AccessContext) -> int: ...

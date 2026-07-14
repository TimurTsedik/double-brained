from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    MarkProcessingNoticeSentCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingCompletionTarget,
    ProcessingNoticeClaim,
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

    async def complete_voice_download(
        self, command: CompleteVoiceDownloadCommand
    ) -> ProcessingStep: ...

    async def lock_transcription_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget: ...

    async def complete_voice_transcription(
        self, command: CompleteVoiceTranscriptionCommand
    ) -> ProcessingStep: ...

    async def claim_due_notice(
        self, access_context: AccessContext, now: datetime
    ) -> ProcessingNoticeClaim | None: ...

    async def mark_notice_sent(
        self, command: MarkProcessingNoticeSentCommand
    ) -> None: ...


class ProcessingQueue(Protocol):
    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProcessingStepClaim | None: ...

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep: ...

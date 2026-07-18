from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    CreateImageProcessingRunCommand,
    CreateTextProcessingRunCommand,
    CreateVoiceProcessingRunCommand,
    FailProcessingStepCommand,
    MarkProcessingNoticeSentCommand,
    SkipProcessingStepCommand,
    SucceedProcessingStepCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingCompletionTarget,
    ProcessingNoticeClaim,
    ProcessingRun,
    ProcessingStep,
    ProcessingStepClaim,
    ProcessingStepType,
)


class ProcessingRepository(Protocol):
    async def create_voice_run(
        self, command: CreateVoiceProcessingRunCommand
    ) -> ProcessingRun: ...

    async def create_text_run(
        self, command: CreateTextProcessingRunCommand
    ) -> ProcessingRun: ...

    async def create_image_run(
        self, command: CreateImageProcessingRunCommand
    ) -> ProcessingRun: ...

    async def claim_due_step(
        self,
        access_context: AccessContext,
        now: datetime,
        lease_duration: timedelta,
        step_types: tuple[ProcessingStepType, ...],
    ) -> ProcessingStepClaim | None: ...

    async def succeed_step(
        self, command: SucceedProcessingStepCommand
    ) -> ProcessingStep: ...

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep: ...

    async def skip_step(self, command: SkipProcessingStepCommand) -> ProcessingStep: ...

    async def get_run(
        self, access_context: AccessContext, run_id: UUID
    ) -> ProcessingRun | None: ...

    async def count_runs(self, access_context: AccessContext) -> int: ...

    async def complete_voice_download(
        self, command: CompleteVoiceDownloadCommand
    ) -> ProcessingStep: ...

    async def complete_image_download(
        self, command: CompleteImageDownloadCommand
    ) -> ProcessingStep: ...

    async def lock_transcription_target(
        self, access_context: AccessContext, step_id: UUID
    ) -> ProcessingCompletionTarget: ...

    async def lock_indexing_target(
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
        step_types: tuple[ProcessingStepType, ...],
    ) -> ProcessingStepClaim | None: ...

    async def fail_step(self, command: FailProcessingStepCommand) -> ProcessingStep: ...

    async def skip_step(self, command: SkipProcessingStepCommand) -> ProcessingStep: ...

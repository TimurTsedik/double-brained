from typing import Protocol

from second_brain.slices.processing.application.contracts import (
    CompleteVoiceDownloadCommand,
    CompleteVoiceTranscriptionCommand,
    DownloadedVoice,
    DownloadVoiceCommand,
    SendProcessingNoticeCommand,
)


class VoiceDownloader(Protocol):
    async def download(self, command: DownloadVoiceCommand) -> DownloadedVoice: ...


class VoiceDownloadCompletion(Protocol):
    async def complete(self, command: CompleteVoiceDownloadCommand) -> None: ...


class VoiceTranscriptionCompletion(Protocol):
    async def complete(self, command: CompleteVoiceTranscriptionCommand) -> None: ...


class ProcessingNotifier(Protocol):
    async def send(self, command: SendProcessingNoticeCommand) -> None: ...

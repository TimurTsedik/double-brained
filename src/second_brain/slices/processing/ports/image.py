from typing import Protocol

from second_brain.slices.processing.application.contracts import (
    CompleteImageDownloadCommand,
    DownloadedImage,
    DownloadImageCommand,
)


class ImageDownloader(Protocol):
    async def download(self, command: DownloadImageCommand) -> DownloadedImage: ...


class ImageDownloadCompletion(Protocol):
    async def complete(self, command: CompleteImageDownloadCommand) -> None: ...

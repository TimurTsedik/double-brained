"""Скачивание оригинала фото из Telegram по file_id (зеркалит голосовой путь)."""

from io import BytesIO

from aiogram import Bot

from second_brain.slices.processing.application.contracts import (
    DownloadedImage,
    DownloadImageCommand,
)


class TelegramImageDownloadError(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class AiogramImageDownloader:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def download(self, command: DownloadImageCommand) -> DownloadedImage:
        try:
            telegram_file = await self._bot.get_file(command.file_id)
            if telegram_file.file_path is None:
                raise TelegramImageDownloadError("telegram_file_path_missing")
            destination = BytesIO()
            await self._bot.download_file(
                telegram_file.file_path,
                destination=destination,
            )
            return DownloadedImage(content=destination.getvalue())
        except TelegramImageDownloadError:
            raise
        except Exception:
            raise TelegramImageDownloadError("telegram_download_failed") from None

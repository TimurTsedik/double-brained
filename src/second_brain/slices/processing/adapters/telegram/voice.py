from io import BytesIO

from aiogram import Bot

from second_brain.slices.processing.adapters.telegram.messages import notice_text
from second_brain.slices.processing.application.contracts import (
    DownloadedVoice,
    DownloadVoiceCommand,
    SendProcessingNoticeCommand,
)

DEFAULT_VOICE_MIME_TYPE = "audio/ogg"


class TelegramVoiceDownloadError(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class AiogramVoiceDownloader:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def download(self, command: DownloadVoiceCommand) -> DownloadedVoice:
        try:
            telegram_file = await self._bot.get_file(command.file_id)
            if telegram_file.file_path is None:
                raise TelegramVoiceDownloadError("telegram_file_path_missing")
            destination = BytesIO()
            await self._bot.download_file(
                telegram_file.file_path,
                destination=destination,
            )
            return DownloadedVoice(
                content=destination.getvalue(),
                mime_type=command.mime_type or DEFAULT_VOICE_MIME_TYPE,
            )
        except TelegramVoiceDownloadError:
            raise
        except Exception:
            raise TelegramVoiceDownloadError("telegram_download_failed") from None


class AiogramVoiceNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, command: SendProcessingNoticeCommand) -> None:
        text = notice_text(command.notice, command.locale)
        await self._bot.send_message(command.recipient_telegram_id, text)

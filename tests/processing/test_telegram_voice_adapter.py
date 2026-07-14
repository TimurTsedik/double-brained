from io import BytesIO
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from aiogram import Bot

from second_brain.slices.processing.adapters.telegram.voice import (
    AiogramVoiceDownloader,
    AiogramVoiceNotifier,
    TelegramVoiceDownloadError,
)
from second_brain.slices.processing.application.contracts import (
    DownloadVoiceCommand,
    SendProcessingNoticeCommand,
)
from second_brain.slices.processing.domain.entities import (
    ProcessingNoticeClaim,
    ProcessingNoticeKind,
    TranscriptionOutputType,
)


class FakeBot:
    def __init__(self, *, fail_download: bool = False) -> None:
        self.fail_download = fail_download
        self.file_ids: list[str] = []
        self.paths: list[str] = []
        self.messages: list[tuple[int, str]] = []

    async def get_file(self, file_id: str) -> object:
        self.file_ids.append(file_id)
        if self.fail_download:
            raise RuntimeError("provider response contains private-file-id")
        return SimpleNamespace(file_path="voice/file.oga")

    async def download_file(self, file_path: str, destination: BytesIO) -> BytesIO:
        self.paths.append(file_path)
        destination.write(b"voice bytes")
        return destination

    async def send_message(self, chat_id: int, text: str) -> object:
        self.messages.append((chat_id, text))
        return object()


@pytest.mark.asyncio
async def test_downloader_uses_stored_file_id_and_returns_bytes() -> None:
    bot = FakeBot()
    downloader = AiogramVoiceDownloader(cast(Bot, bot))

    downloaded = await downloader.download(
        DownloadVoiceCommand(file_id="private-file-id", mime_type=None)
    )

    assert bot.file_ids == ["private-file-id"]
    assert bot.paths == ["voice/file.oga"]
    assert downloaded.content == b"voice bytes"
    assert downloaded.mime_type == "audio/ogg"
    assert "voice bytes" not in repr(downloaded)


@pytest.mark.asyncio
async def test_downloader_converts_provider_error_to_safe_failure() -> None:
    downloader = AiogramVoiceDownloader(cast(Bot, FakeBot(fail_download=True)))

    with pytest.raises(TelegramVoiceDownloadError) as caught:
        await downloader.download(
            DownloadVoiceCommand(file_id="private-file-id", mime_type="audio/ogg")
        )

    assert caught.value.safe_error_code == "telegram_download_failed"
    assert "private-file-id" not in repr(caught.value)


def notice(
    kind: ProcessingNoticeKind,
    output_type: TranscriptionOutputType = TranscriptionOutputType.NOTE,
) -> ProcessingNoticeClaim:
    return ProcessingNoticeClaim(
        notice_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        kind=kind,
        output_type=output_type,
        trace_id="a" * 32,
        attempt_count=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_type", "label"),
    [
        (TranscriptionOutputType.NOTE, "📝 Заметка"),
        (TranscriptionOutputType.TASK, "✅ Задача"),
        (TranscriptionOutputType.IDEA, "💡 Идея"),
        (TranscriptionOutputType.DECISION, "⚖️ Решение"),
        (TranscriptionOutputType.QUESTION, "❓ Вопрос"),
    ],
)
async def test_success_notifier_sends_only_fixed_type_status(
    output_type: TranscriptionOutputType, label: str
) -> None:
    bot = FakeBot()
    notifier = AiogramVoiceNotifier(cast(Bot, bot))

    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=555,
            notice=notice(ProcessingNoticeKind.SUCCESS, output_type),
        )
    )

    assert bot.messages == [(555, f"🎙️ Расшифровано и сохранено: {label}.")]


@pytest.mark.asyncio
async def test_failure_notifier_sends_only_safe_trace_id() -> None:
    bot = FakeBot()
    notifier = AiogramVoiceNotifier(cast(Bot, bot))

    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=555,
            notice=notice(ProcessingNoticeKind.FAILURE),
        )
    )

    assert bot.messages == [
        (
            555,
            "Не удалось обработать голосовое сообщение.\nTrace ID: " + "a" * 32,
        )
    ]

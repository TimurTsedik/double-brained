import ast
import string
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from aiogram import Bot

import second_brain.slices.processing.adapters.telegram.voice as voice_module
from second_brain.shared.i18n import Locale
from second_brain.slices.processing.adapters.telegram import messages
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


def _placeholders(template: str) -> set[str]:
    return {
        field
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    }


# ---------------------------------------------------------------------------
# catalog coverage + placeholder parity (decision 10)
# ---------------------------------------------------------------------------


def test_every_output_type_has_a_success_label_in_both_locales() -> None:
    for output_type in TranscriptionOutputType:
        for locale in Locale:
            assert messages.SUCCESS_LABELS[output_type][locale].strip()


def test_every_notice_kind_has_text_in_both_locales() -> None:
    assert messages.CATALOG
    for kind in ProcessingNoticeKind:
        for locale in Locale:
            text = messages.notice_text(notice(kind), locale)
            assert text.strip()


def test_catalog_placeholder_sets_match_across_locales() -> None:
    for key, translations in messages.CATALOG.items():
        placeholder_sets = {
            locale: _placeholders(text) for locale, text in translations.items()
        }
        reference = placeholder_sets[Locale.RU]
        for locale, found in placeholder_sets.items():
            assert found == reference, (
                f"{key}: {locale} placeholders {found} != {reference}"
            )
        for locale in Locale:
            assert locale in translations, f"{key} missing {locale}"


# ---------------------------------------------------------------------------
# anti-hardcode scan of voice.py (decision 10, third module)
# ---------------------------------------------------------------------------


def _hardcoded_send_message_text(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else None
        if name != "send_message":
            continue
        args: list[ast.expr] = list(node.args)
        args.extend(keyword.value for keyword in node.keywords)
        for value in args:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                offenders.append((value.value, node.lineno))
    return offenders


def test_voice_adapter_has_no_hardcoded_user_text() -> None:
    path = Path(cast(str, voice_module.__file__))
    assert _hardcoded_send_message_text(path) == []


# ---------------------------------------------------------------------------
# notifier regression (RU) + EN via command.locale
# ---------------------------------------------------------------------------


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
async def test_success_notifier_ru_regression_keeps_current_strings(
    output_type: TranscriptionOutputType, label: str
) -> None:
    bot = FakeBot()
    notifier = AiogramVoiceNotifier(cast(Bot, bot))

    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=555,
            notice=notice(ProcessingNoticeKind.SUCCESS, output_type),
            locale=Locale.RU,
        )
    )

    assert bot.messages == [(555, f"🎙️ Расшифровано и сохранено: {label}.")]


@pytest.mark.asyncio
async def test_success_notifier_en_is_english() -> None:
    bot = FakeBot()
    notifier = AiogramVoiceNotifier(cast(Bot, bot))

    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=555,
            notice=notice(ProcessingNoticeKind.SUCCESS, TranscriptionOutputType.NOTE),
            locale=Locale.EN,
        )
    )

    en_label = messages.SUCCESS_LABELS[TranscriptionOutputType.NOTE][Locale.EN]
    expected = messages.CATALOG["notice.success"][Locale.EN].format(label=en_label)
    assert bot.messages == [(555, expected)]
    assert "Расшифровано" not in bot.messages[0][1]


@pytest.mark.asyncio
async def test_failure_notifier_ru_regression_keeps_current_strings() -> None:
    bot = FakeBot()
    notifier = AiogramVoiceNotifier(cast(Bot, bot))

    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=555,
            notice=notice(ProcessingNoticeKind.FAILURE),
            locale=Locale.RU,
        )
    )

    assert bot.messages == [
        (
            555,
            "Не удалось обработать запись.\nTrace ID: " + "a" * 32,
        )
    ]


@pytest.mark.asyncio
async def test_failure_notifier_en_carries_trace_without_content() -> None:
    bot = FakeBot()
    notifier = AiogramVoiceNotifier(cast(Bot, bot))

    await notifier.send(
        SendProcessingNoticeCommand(
            recipient_telegram_id=555,
            notice=notice(ProcessingNoticeKind.FAILURE),
            locale=Locale.EN,
        )
    )

    expected = messages.CATALOG["notice.failure"][Locale.EN].format(trace_id="a" * 32)
    assert bot.messages == [(555, expected)]
    assert "Не удалось" not in bot.messages[0][1]
    assert "a" * 32 in bot.messages[0][1]

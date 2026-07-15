import ast
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot

import second_brain.slices.identity.adapters.telegram.gateway as gateway_module
import second_brain.slices.identity.adapters.telegram.poller as poller_module
from second_brain.shared.i18n import Locale
from second_brain.slices.identity.adapters.telegram import messages
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.projects.application.contracts import (
    ProjectListItem,
    ProjectPanelResult,
)
from second_brain.slices.retrieval.application.contracts import SearchPanelResult
from second_brain.slices.retrieval.domain.entities import (
    MatchQuality,
    SearchRecord,
    SearchRecordType,
)
from second_brain.slices.tasks.application.contracts import (
    TaskListItem,
    TaskPanelResult,
)
from tests.identity.locale_fakes import FakeLocaleResolver

NOW = datetime(2026, 7, 15, tzinfo=UTC)


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)


def _gateway(locale: Locale) -> tuple[RecordingAiogramBot, AiogramGateway]:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver(locale)
    )
    return bot, gateway


def _callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def _text(update_id: int, text: str) -> TelegramUpdate:
    return TelegramUpdate(1, update_id, True, 42, text)


def _placeholders(template: str) -> set[str]:
    return {
        field
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    }


# ---------------------------------------------------------------------------
# (а) enum / key coverage
# ---------------------------------------------------------------------------


def test_every_catalog_key_has_both_locales() -> None:
    assert messages.CATALOG, "catalog must not be empty"
    for key, translations in messages.CATALOG.items():
        for locale in Locale:
            assert locale in translations, f"{key} missing {locale}"
            assert translations[locale].strip(), f"{key}/{locale} is blank"


def test_every_user_acknowledgement_kind_is_translated() -> None:
    assert messages.USER_ACKNOWLEDGEMENT_KINDS
    for kind in messages.USER_ACKNOWLEDGEMENT_KINDS:
        for locale in Locale:
            assert messages.acknowledgement_text(kind, locale).strip()


def test_every_selection_callback_is_translated() -> None:
    assert messages.SELECTION_CALLBACKS
    for callback_data in messages.SELECTION_CALLBACKS:
        for locale in Locale:
            text = messages.selection_feedback_text(callback_data, locale)
            assert text is not None and text.strip()


def test_every_search_record_type_has_a_label_in_both_locales() -> None:
    for record_type in SearchRecordType:
        for completed in (False, True):
            record = _search_record(record_type, completed)
            for locale in Locale:
                assert messages.search_label(record, locale).strip()


# ---------------------------------------------------------------------------
# (в) placeholder parity
# ---------------------------------------------------------------------------


def test_placeholder_sets_match_across_locales() -> None:
    for key, translations in messages.CATALOG.items():
        placeholder_sets = {
            locale: _placeholders(text) for locale, text in translations.items()
        }
        reference = placeholder_sets[Locale.RU]
        for locale, found in placeholder_sets.items():
            assert found == reference, (
                f"{key}: {locale} placeholders {found} != {reference}"
            )


# ---------------------------------------------------------------------------
# (б) anti-hardcode scan of gateway.py and poller.py
#
# Граница «пользовательский текст vs формат»: литерал в text= считается
# пользовательским тогда и только тогда, когда содержит АЛФАВИТНЫЙ символ
# (буква любого алфавита). Чисто-форматные литералы — эмодзи, цифры-плейсхолдер,
# пунктуация, перевод строки — букв не содержат и проходят (например «✅ {number}»,
# «✓ », «…», «\n\n»). Так будущая переводимая f-строка (с буквами) мимо каталога
# роняет тест, а текущие нейтральные форматы — нет. Ловим и ast.Constant, и
# ast.JoinedStr (f-строки): раньше f-строки проскакивали.
# ---------------------------------------------------------------------------

_SEND_TEXT_CALLS = {"send_message", "InlineKeyboardButton"}


def _has_letters(value: str) -> bool:
    return any(character.isalpha() for character in value)


def _literal_parts(value: ast.expr) -> list[str]:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.JoinedStr):
        return [
            part.value
            for part in value.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def _hardcoded_user_text_in_source(source: str) -> list[tuple[str, int]]:
    tree = ast.parse(source)
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name not in _SEND_TEXT_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg != "text":
                continue
            for part in _literal_parts(keyword.value):
                if _has_letters(part):
                    offenders.append((part, node.lineno))
    return offenders


def _hardcoded_user_text(path: Path) -> list[tuple[str, int]]:
    return _hardcoded_user_text_in_source(path.read_text(encoding="utf-8"))


def test_gateway_has_no_hardcoded_user_text() -> None:
    path = Path(cast(str, gateway_module.__file__))
    assert _hardcoded_user_text(path) == []


def test_poller_has_no_hardcoded_user_text() -> None:
    path = Path(cast(str, poller_module.__file__))
    assert _hardcoded_user_text(path) == []


def test_scan_flags_a_translatable_fstring() -> None:
    # Искусственная переводимая f-строка мимо каталога — должна ловиться.
    source = 'bot.send_message(chat_id=1, text=f"Привет, {name}!")'
    assert _hardcoded_user_text_in_source(source)


def test_scan_flags_a_hardcoded_plain_literal() -> None:
    source = 'bot.send_message(chat_id=1, text="Enrollment complete.")'
    assert _hardcoded_user_text_in_source(source)


def test_scan_allows_pure_format_fstring() -> None:
    # Эмодзи + числовой плейсхолдер без букв — это формат, не текст.
    source = 'InlineKeyboardButton(text=f"✅ {number}", callback_data="x")'
    assert _hardcoded_user_text_in_source(source) == []


# ---------------------------------------------------------------------------
# gateway regression (RU) + EN via injected resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_ru_matches_today_and_carries_language_button() -> None:
    bot, gateway = _gateway(Locale.RU)

    await gateway.send_panel(_text(1, "/start"))

    message = bot.sent_messages[0]
    assert message["text"] == "Выберите действие."
    markup = message["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        "tasks:list",
        "search:prompt",
        "memory:ask",
        "projects:list",
    ]
    assert [b.callback_data for b in markup.inline_keyboard[1]] == [
        "capture:note",
        "capture:task",
        "capture:idea",
    ]
    assert [b.callback_data for b in markup.inline_keyboard[2]] == [
        "capture:decision",
        "capture:question",
        "capture:cancel",
    ]
    lang_row = markup.inline_keyboard[3]
    assert [b.callback_data for b in lang_row] == ["lang:menu"]
    assert lang_row[0].text == "🌐 Язык / Language"
    assert [b.text for b in markup.inline_keyboard[0]] == [
        "📋 Мои задачи",
        "🔎 Поиск",
        "🧠 Спросить память",
        "📁 Проекты",
    ]


@pytest.mark.asyncio
async def test_panel_en_is_english_with_same_layout() -> None:
    bot, gateway = _gateway(Locale.EN)

    await gateway.send_panel(_text(1, "/start"))

    message = bot.sent_messages[0]
    assert message["text"] == messages.CATALOG["panel.prompt"][Locale.EN]
    assert message["text"] != "Выберите действие."
    markup = message["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        "tasks:list",
        "search:prompt",
        "memory:ask",
        "projects:list",
    ]
    assert markup.inline_keyboard[3][0].callback_data == "lang:menu"
    assert markup.inline_keyboard[3][0].text == "🌐 Язык / Language"


@pytest.mark.asyncio
async def test_voice_queued_ru_regression_and_en() -> None:
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_voice_queued(_text(1, "voice"))
    assert bot_ru.sent_messages[0]["text"] == "🎙️ Голос сохранён. Расшифровываю…"

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_voice_queued(_text(1, "voice"))
    assert (
        bot_en.sent_messages[0]["text"] == messages.CATALOG["voice_queued"][Locale.EN]
    )
    assert bot_en.sent_messages[0]["text"] != "🎙️ Голос сохранён. Расшифровываю…"


@pytest.mark.asyncio
async def test_search_prompt_ru_regression_and_en() -> None:
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_search_prompt(
        _callback(1, "search:prompt"), query_required=False
    )
    assert bot_ru.sent_messages[0]["text"] == (
        "🔎 Что найти?\n\n"
        "Отправьте слово или фразу. Следующее сообщение станет запросом, "
        "а не новой записью."
    )
    assert (
        bot_ru.sent_messages[0]["reply_markup"].inline_keyboard[0][0].text == "✖️ Отмена"
    )

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_search_prompt(
        _callback(1, "search:prompt"), query_required=False
    )
    assert (
        bot_en.sent_messages[0]["text"]
        == messages.CATALOG["search_prompt.intro"][Locale.EN]
    )
    assert "Что найти" not in bot_en.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_search_panel_header_counts_in_both_locales() -> None:
    result = SearchPanelResult(
        (_search_record(SearchRecordType.NOTE, False),), query_required=False
    )
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_search_panel(_text(1, "q"), result)
    assert bot_ru.sent_messages[0]["text"].startswith("🔎 Найдено: 1")

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_search_panel(_text(1, "q"), result)
    assert bot_en.sent_messages[0]["text"].startswith(
        messages.CATALOG["search_panel.found"][Locale.EN].format(count=1)
    )


@pytest.mark.asyncio
async def test_task_panel_header_in_both_locales() -> None:
    result = TaskPanelResult(
        (TaskListItem(id=UUID(int=1), title="Do it"),),
        completion_changed=None,
    )
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_task_panel(_text(1, "x"), result, is_completion=False)
    assert bot_ru.sent_messages[0]["text"].startswith("📋 Открытые задачи")

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_task_panel(_text(1, "x"), result, is_completion=False)
    assert bot_en.sent_messages[0]["text"].startswith(
        messages.CATALOG["task_panel.header"][Locale.EN]
    )


@pytest.mark.asyncio
async def test_project_panel_announcement_in_both_locales() -> None:
    result = ProjectPanelResult(
        items=(ProjectListItem(id=UUID(int=7), name="Alpha"),),
        current_project_id=UUID(int=7),
        action_succeeded=True,
    )
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_project_panel(
        _text(1, "x"), result, AcknowledgementKind.PROJECT_CREATED
    )
    assert "Проект выбран" in bot_ru.sent_messages[0]["text"]

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_project_panel(
        _text(1, "x"), result, AcknowledgementKind.PROJECT_CREATED
    )
    assert "Проект выбран" not in bot_en.sent_messages[0]["text"]
    assert (
        messages.CATALOG["project_panel.body"][Locale.EN].split("\n")[0]
        in (bot_en.sent_messages[0]["text"])
    )


@pytest.mark.asyncio
async def test_selection_feedback_in_both_locales() -> None:
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_selection_feedback(_callback(1, "capture:note"))
    assert bot_ru.sent_messages[0]["text"] == "📝 Заметка"

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_selection_feedback(_callback(1, "capture:note"))
    assert bot_en.sent_messages[0]["text"] == messages.selection_feedback_text(
        "capture:note", Locale.EN
    )
    assert bot_en.sent_messages[0]["text"] != "📝 Заметка"


# ---------------------------------------------------------------------------
# duplicate path: gateway resolves locale from DB, not payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_acknowledgement_is_localized_from_resolver() -> None:
    # A fallback ack (poller sends send_acknowledgement even when fresh=False)
    # must still be localized because the gateway resolves the locale itself.
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_acknowledgement(
        _text(1, "q"), AcknowledgementKind.MEMORY_QUESTION_QUEUED
    )
    assert bot_ru.sent_messages[0]["text"] == "⏳ Готовлю ответ…"

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_acknowledgement(
        _text(1, "q"), AcknowledgementKind.MEMORY_QUESTION_QUEUED
    )
    assert bot_en.sent_messages[0]["text"] == messages.acknowledgement_text(
        AcknowledgementKind.MEMORY_QUESTION_QUEUED, Locale.EN
    )
    assert bot_en.sent_messages[0]["text"] != "⏳ Готовлю ответ…"


def _search_record(record_type: SearchRecordType, task_completed: bool) -> SearchRecord:
    return SearchRecord(
        id=UUID(int=1),
        record_type=record_type,
        text="text",
        source_capture_event_id=UUID(int=2),
        created_at=NOW,
        task_completed=task_completed,
        match_quality=MatchQuality.SUBSTRING,
    )


# ---------------------------------------------------------------------------
# weave-in 1: entry acks are proper Russian in RU (English in EN)
# ---------------------------------------------------------------------------


def test_entry_acknowledgements_are_russian_in_ru_and_english_in_en() -> None:
    expected_ru = {
        AcknowledgementKind.ENROLLED: "Готово, доступ открыт.",
        AcknowledgementKind.ENROLLMENT_REJECTED: "Не удалось открыть доступ.",
        AcknowledgementKind.KNOWN_USER_STARTED: "С возвращением.",
    }
    for kind, russian in expected_ru.items():
        assert messages.acknowledgement_text(kind, Locale.RU) == russian
        english = messages.acknowledgement_text(kind, Locale.EN)
        assert english != russian
        assert english.isascii()


# ---------------------------------------------------------------------------
# language chooser + selection confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [Locale.RU, Locale.EN])
async def test_language_chooser_is_bilingual_with_lang_buttons(locale: Locale) -> None:
    bot, gateway = _gateway(locale)

    await gateway.send_language_prompt(_callback(1, "lang:menu"))

    message = bot.sent_messages[0]
    # Chooser is bilingual by nature (language not chosen yet), identical text
    # regardless of the resolved locale.
    assert "/" in message["text"]
    assert message["text"] == messages.CATALOG["language.chooser"][Locale.RU]
    markup = message["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        "lang:ru",
        "lang:en",
    ]


@pytest.mark.asyncio
async def test_language_selected_renders_in_the_resolved_locale() -> None:
    bot_ru, gateway_ru = _gateway(Locale.RU)
    await gateway_ru.send_language_selected(_callback(1, "lang:ru"))
    assert (
        bot_ru.sent_messages[0]["text"]
        == messages.CATALOG["language.selected"][Locale.RU]
    )
    assert not bot_ru.sent_messages[0]["text"].isascii()

    bot_en, gateway_en = _gateway(Locale.EN)
    await gateway_en.send_language_selected(_callback(1, "lang:en"))
    assert (
        bot_en.sent_messages[0]["text"]
        == messages.CATALOG["language.selected"][Locale.EN]
    )
    assert bot_en.sent_messages[0]["text"].isascii()


# ---------------------------------------------------------------------------
# poller dispatch of the new kinds
# ---------------------------------------------------------------------------


class _SpyGateway:
    bot_id = 1

    def __init__(self, kind: AcknowledgementKind) -> None:
        self._kind = kind
        self.calls: list[str] = []

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        if offset is not None:
            return []
        return [_callback(1, "lang:menu")]

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.calls.append("answer_callback")

    async def send_language_prompt(self, update: TelegramUpdate) -> None:
        self.calls.append("send_language_prompt")

    async def send_language_selected(self, update: TelegramUpdate) -> None:
        self.calls.append("send_language_selected")

    async def send_panel(self, update: TelegramUpdate) -> None:
        self.calls.append("send_panel")

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append("send_acknowledgement")


class _KindProcessor:
    def __init__(self, kind: AcknowledgementKind) -> None:
        self._kind = kind

    async def process(self, update: TelegramUpdate) -> Any:
        return type("R", (), {"kind": self._kind, "fresh": True})()


class _AlwaysLock:
    async def acquire(self, bot_id: int) -> bool:
        return True


@pytest.mark.asyncio
async def test_poller_dispatches_language_prompt() -> None:
    gateway = _SpyGateway(AcknowledgementKind.LANGUAGE_PROMPT_SHOWN)
    poller = poller_module.LocalPoller(
        gateway,  # type: ignore[arg-type]
        _KindProcessor(AcknowledgementKind.LANGUAGE_PROMPT_SHOWN),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert "send_language_prompt" in gateway.calls
    assert "send_acknowledgement" not in gateway.calls


@pytest.mark.asyncio
async def test_poller_dispatches_language_selected_then_panel() -> None:
    gateway = _SpyGateway(AcknowledgementKind.LANGUAGE_SELECTED)
    poller = poller_module.LocalPoller(
        gateway,  # type: ignore[arg-type]
        _KindProcessor(AcknowledgementKind.LANGUAGE_SELECTED),
        _AlwaysLock(),
    )

    await poller.run_once()

    assert gateway.calls.count("send_language_selected") == 1
    assert gateway.calls.count("send_panel") == 1
    assert gateway.calls.index("send_language_selected") < gateway.calls.index(
        "send_panel"
    )
    assert "send_acknowledgement" not in gateway.calls

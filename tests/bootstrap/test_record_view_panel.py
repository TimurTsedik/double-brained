"""Транспорт «показать целиком»: кнопки под поиском, send_record_view, поллер.

Сплит считает ВЕСЬ исходящий текст против 4096: части режутся по границе
строки/слова (патологический токен — жёсткий срез), секция «похожего» и её
кнопки — только на ПОСЛЕДНЕЙ части. Заголовок «{label} · {date}» локализован.
"""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot

from second_brain.shared.i18n import Locale
from second_brain.slices.identity.adapters.telegram import messages
from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from second_brain.slices.identity.adapters.telegram.poller import LocalPoller
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    UpdateResult,
)
from second_brain.slices.retrieval.application.contracts import (
    RecordView,
    RecordViewResult,
    SearchPanelResult,
)
from second_brain.slices.retrieval.domain.entities import (
    MatchQuality,
    SearchRecord,
    SearchRecordType,
)
from tests.identity.locale_fakes import FakeLocaleResolver

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
TELEGRAM_LIMIT = 4096


class RecordingAiogramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent_messages.append(kwargs)


def gateway_with_bot(
    locale: Locale = Locale.RU,
) -> tuple[RecordingAiogramBot, AiogramGateway]:
    bot = RecordingAiogramBot()
    gateway = AiogramGateway(
        cast(Bot, bot), bot_id=1, locale_resolver=FakeLocaleResolver(locale)
    )
    return bot, gateway


def callback(update_id: int, data: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        None,
        callback_query_id=f"callback-{update_id}",
        callback_data=data,
    )


def search_record(number: int, record_type: SearchRecordType) -> SearchRecord:
    return SearchRecord(
        id=UUID(int=number),
        record_type=record_type,
        text=f"result {number}",
        source_capture_event_id=UUID(int=number + 100),
        created_at=NOW,
        task_completed=False if record_type is SearchRecordType.TASK else None,
        match_quality=MatchQuality.SUBSTRING,
    )


def record_view(
    number: int,
    text: str,
    record_type: SearchRecordType = SearchRecordType.NOTE,
    task_completed: bool | None = None,
) -> RecordView:
    return RecordView(
        id=UUID(int=number),
        record_type=record_type,
        text=text,
        created_at=NOW,
        task_completed=task_completed,
    )


# ---------------------------------------------------------------------------
# панель поиска: номерные кнопки ведут на верные записи
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_panel_numbered_buttons_target_the_right_records() -> None:
    bot, gateway = gateway_with_bot()
    records = tuple(
        search_record(number, record_type)
        for number, record_type in enumerate(
            (
                SearchRecordType.NOTE,
                SearchRecordType.TASK,
                SearchRecordType.IDEA,
                SearchRecordType.DECISION,
                SearchRecordType.QUESTION,
                SearchRecordType.NOTE,
            ),
            start=1,
        )
    )

    await gateway.send_search_panel(
        callback(700, "search:prompt"),
        SearchPanelResult(records, query_required=False),
    )

    markup = bot.sent_messages[0]["reply_markup"]
    number_buttons = [button for row in markup.inline_keyboard[:-1] for button in row]
    assert [button.text for button in number_buttons] == ["1", "2", "3", "4", "5", "6"]
    assert [button.callback_data for button in number_buttons] == [
        f"show:{record.record_type.value}:{record.id}" for record in records
    ]
    assert all(len(button.callback_data.encode()) <= 64 for button in number_buttons)
    assert [button.callback_data for button in markup.inline_keyboard[-1]] == [
        "search:prompt"
    ]


@pytest.mark.asyncio
async def test_empty_search_panel_keeps_only_the_search_again_button() -> None:
    bot, gateway = gateway_with_bot()

    await gateway.send_search_panel(
        callback(701, "search:prompt"),
        SearchPanelResult((), query_required=False),
    )

    markup = bot.sent_messages[0]["reply_markup"]
    assert [
        [button.callback_data for button in row] for row in markup.inline_keyboard
    ] == [["search:prompt"]]


# ---------------------------------------------------------------------------
# send_record_view: заголовок, сплит, секция похожего
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_view_sends_header_full_text_and_related_with_buttons() -> None:
    bot, gateway = gateway_with_bot()
    related = (
        record_view(2, "похожая заметка"),
        record_view(3, "сделанная задача", SearchRecordType.TASK, task_completed=True),
    )
    result = RecordViewResult(
        record=record_view(1, "полный текст записи"), related=related
    )

    await gateway.send_record_view(callback(702, f"show:note:{UUID(int=1)}"), result)

    assert len(bot.sent_messages) == 1
    message = bot.sent_messages[0]
    assert message["text"].startswith(
        "📝 Заметка · 15.07.2026\n\nполный текст записи\n\n🧬 Похожее по смыслу:\n\n"
    )
    assert "1. 📝 Заметка\nпохожая заметка" in message["text"]
    assert "2. ☑️ Завершённая задача\nсделанная задача" in message["text"]
    markup = message["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["1", "2"]
    assert [button.callback_data for button in buttons] == [
        f"show:note:{UUID(int=2)}",
        f"show:task:{UUID(int=3)}",
    ]


@pytest.mark.asyncio
async def test_record_view_without_related_has_no_section_and_no_buttons() -> None:
    bot, gateway = gateway_with_bot()
    result = RecordViewResult(record=record_view(1, "просто текст"), related=())

    await gateway.send_record_view(callback(703, f"show:note:{UUID(int=1)}"), result)

    assert len(bot.sent_messages) == 1
    message = bot.sent_messages[0]
    assert message["text"] == "📝 Заметка · 15.07.2026\n\nпросто текст"
    assert "reply_markup" not in message


@pytest.mark.asyncio
async def test_record_view_header_is_localized_in_english() -> None:
    bot, gateway = gateway_with_bot(Locale.EN)
    result = RecordViewResult(
        record=record_view(1, "full text"), related=(record_view(2, "related"),)
    )

    await gateway.send_record_view(callback(704, f"show:note:{UUID(int=1)}"), result)

    text = bot.sent_messages[0]["text"]
    assert text.startswith("📝 Note · 15.07.2026\n\nfull text")
    assert messages.CATALOG["record_view.related_header"][Locale.EN] in text
    assert "Похожее" not in text


@pytest.mark.asyncio
async def test_record_view_header_shows_the_completed_task_label() -> None:
    bot, gateway = gateway_with_bot()
    result = RecordViewResult(
        record=record_view(
            1, "текст задачи", SearchRecordType.TASK, task_completed=True
        ),
        related=(),
    )

    await gateway.send_record_view(callback(705, f"show:task:{UUID(int=1)}"), result)

    assert bot.sent_messages[0]["text"] == (
        "☑️ Завершённая задача · 15.07.2026\n\nтекст задачи"
    )


@pytest.mark.asyncio
async def test_long_record_splits_without_midword_cuts_and_related_only_last() -> None:
    bot, gateway = gateway_with_bot()
    words = [f"w{number:04d}" for number in range(1500)]
    result = RecordViewResult(
        record=record_view(1, " ".join(words)),
        related=(record_view(2, "похожая", SearchRecordType.IDEA),),
    )

    await gateway.send_record_view(callback(706, f"show:note:{UUID(int=1)}"), result)

    texts = [message["text"] for message in bot.sent_messages]
    assert len(texts) > 1
    assert all(len(text) <= TELEGRAM_LIMIT for text in texts)
    # Ни одного разрыва посреди слова: каждый w-токен цел, порядок и состав
    # соответствуют исходному тексту.
    word_tokens = [
        token
        for text in texts
        for token in text.split()
        if token.startswith("w") and token[1:].isdigit()
    ]
    assert word_tokens == words
    # Заголовок — только на первой части; похожее и кнопки — только на последней.
    assert texts[0].startswith("📝 Заметка · 15.07.2026\n\n")
    related_header = messages.CATALOG["record_view.related_header"][Locale.RU]
    assert [related_header in text for text in texts] == (
        [False] * (len(texts) - 1) + [True]
    )
    for message in bot.sent_messages[:-1]:
        assert "reply_markup" not in message
    last_markup = bot.sent_messages[-1]["reply_markup"]
    assert [
        button.callback_data for row in last_markup.inline_keyboard for button in row
    ] == [f"show:idea:{UUID(int=2)}"]


@pytest.mark.asyncio
async def test_pathological_token_longer_than_limit_is_hard_cut() -> None:
    bot, gateway = gateway_with_bot()
    result = RecordViewResult(record=record_view(1, "x" * 9000), related=())

    await gateway.send_record_view(callback(707, f"show:note:{UUID(int=1)}"), result)

    texts = [message["text"] for message in bot.sent_messages]
    assert all(len(text) <= TELEGRAM_LIMIT for text in texts)
    assert sum(text.count("x") for text in texts) == 9000


@pytest.mark.asyncio
async def test_related_section_moves_to_its_own_final_part_when_it_does_not_fit() -> (
    None
):
    bot, gateway = gateway_with_bot()
    result = RecordViewResult(
        record=record_view(1, "a" * 4000),
        related=(record_view(2, "b" * 300),),
    )

    await gateway.send_record_view(callback(708, f"show:note:{UUID(int=1)}"), result)

    assert len(bot.sent_messages) == 2
    first, second = bot.sent_messages
    assert all(len(message["text"]) <= TELEGRAM_LIMIT for message in (first, second))
    assert "🧬" not in first["text"]
    assert "reply_markup" not in first
    assert second["text"].startswith(
        messages.CATALOG["record_view.related_header"][Locale.RU]
    )
    assert second["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"show:note:{UUID(int=2)}"
    )


# ---------------------------------------------------------------------------
# поллер: fresh-only side-effect, replay молчит, generic-ack исключён
# ---------------------------------------------------------------------------


class _RecordViewSpyGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.calls: list[str] = []
        self.record_views: list[RecordViewResult] = []

    async def configured_webhook_url(self) -> str | None:
        return None

    async def get_updates(
        self, offset: int | None, _allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        if offset is not None:
            return []
        return [self._update]

    async def answer_callback(self, update: TelegramUpdate) -> None:
        self.calls.append("answer_callback")

    async def send_record_view(
        self, update: TelegramUpdate, result: RecordViewResult
    ) -> None:
        self.calls.append("send_record_view")
        self.record_views.append(result)

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        self.calls.append("send_acknowledgement")


class _StaticProcessor:
    def __init__(self, result: UpdateResult) -> None:
        self._result = result

    async def process(self, _update: TelegramUpdate) -> UpdateResult:
        return self._result


class _AlwaysLock:
    async def acquire(self, _bot_id: int) -> bool:
        return True


def shown_result(
    *, fresh: bool, record_view_payload: RecordViewResult | None
) -> UpdateResult:
    return UpdateResult(
        AcknowledgementKind.RECORD_SHOWN,
        "1" * 32,
        "2" * 16,
        fresh=fresh,
        record_view=record_view_payload,
    )


@pytest.mark.asyncio
async def test_poller_sends_fresh_record_view_without_generic_ack() -> None:
    update = callback(710, f"show:note:{UUID(int=1)}")
    payload = RecordViewResult(record=record_view(1, "text"), related=())
    gateway = _RecordViewSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(shown_result(fresh=True, record_view_payload=payload)),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback", "send_record_view"]
    assert gateway.record_views == [payload]


@pytest.mark.asyncio
async def test_poller_stays_silent_on_a_record_shown_replay() -> None:
    update = callback(711, f"show:note:{UUID(int=1)}")
    gateway = _RecordViewSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(shown_result(fresh=False, record_view_payload=None)),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback"]


@pytest.mark.asyncio
async def test_poller_requires_a_payload_for_a_fresh_record_shown() -> None:
    update = callback(712, f"show:note:{UUID(int=1)}")
    gateway = _RecordViewSpyGateway(update)

    with pytest.raises(RuntimeError):
        await LocalPoller(
            gateway,  # type: ignore[arg-type]
            _StaticProcessor(shown_result(fresh=True, record_view_payload=None)),
            _AlwaysLock(),
        ).run_once()


@pytest.mark.asyncio
async def test_poller_sends_nothing_for_an_ignored_show_callback() -> None:
    # Спуф/мусор/чужой uuid: callback отвечен, ни одного сообщения — поведение
    # на транспорте неразличимо.
    update = callback(713, f"show:note:{UUID(int=1)}")
    gateway = _RecordViewSpyGateway(update)

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(
            UpdateResult(AcknowledgementKind.IGNORED, "1" * 32, "2" * 16, fresh=True)
        ),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback"]

"""Транспорт сводки: меню периодов, send_digest, пагинация «⬇️ Ещё», поллер.

Бюджет 4096 считается по ВСЕМУ сообщению; при превышении страница ужимается
ЦЕЛЫМИ строками (текст записи не режется посреди слова), а offset следующей
страницы равен числу ФАКТИЧЕСКИ отрендеренных строк. Кнопка «Ещё» несёт снимок
digest:more:<период>:<offset>:<as_of unix> и исчезает на последней странице.
"""

from datetime import datetime
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot

import second_brain.slices.identity.adapters.telegram.gateway as gateway_module
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
    DigestCounters,
    DigestPage,
    DigestPeriod,
    RecordView,
    SearchRecordType,
)
from tests.identity.locale_fakes import FakeLocaleResolver

TZ = ZoneInfo("Asia/Jerusalem")
AS_OF = datetime(2026, 7, 15, 14, 30, tzinfo=TZ)
PERIOD_START = datetime(2026, 7, 13, tzinfo=TZ)
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


def note(number: int, text: str, day: int = 15) -> RecordView:
    return RecordView(
        id=UUID(int=number),
        record_type=SearchRecordType.NOTE,
        text=text,
        created_at=datetime(2026, 7, day, 10, 0, tzinfo=TZ),
        task_completed=None,
    )


def digest_page(
    items: tuple[RecordView, ...],
    total: int,
    offset: int = 0,
    counters: DigestCounters | None = None,
    period: DigestPeriod = DigestPeriod.WEEK,
) -> DigestPage:
    return DigestPage(
        period=period,
        period_start=PERIOD_START,
        as_of=AS_OF,
        offset=offset,
        total=total,
        counters=counters
        or DigestCounters(
            notes=total, tasks=0, tasks_completed=0, ideas=0, decisions=0, questions=0
        ),
        items=items,
    )


def keyboard_buttons(message: dict[str, Any]) -> list[Any]:
    return [button for row in message["reply_markup"].inline_keyboard for button in row]


# ---------------------------------------------------------------------------
# меню периодов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_menu_offers_the_four_calendar_periods() -> None:
    bot, gateway = gateway_with_bot()

    await gateway.send_digest_menu(callback(900, "digest:menu"))

    message = bot.sent_messages[0]
    assert message["text"] == messages.CATALOG["digest.menu.prompt"][Locale.RU]
    buttons = keyboard_buttons(message)
    assert [button.text for button in buttons] == ["Неделя", "Месяц", "Полгода", "Год"]
    assert [button.callback_data for button in buttons] == [
        "digest:week",
        "digest:month",
        "digest:half_year",
        "digest:year",
    ]
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_digest_menu_is_localized_in_english() -> None:
    bot, gateway = gateway_with_bot(Locale.EN)

    await gateway.send_digest_menu(callback(901, "digest:menu"))

    message = bot.sent_messages[0]
    assert message["text"] == messages.CATALOG["digest.menu.prompt"][Locale.EN]
    assert [button.text for button in keyboard_buttons(message)] == [
        "Week",
        "Month",
        "Half-year",
        "Year",
    ]


# ---------------------------------------------------------------------------
# send_digest: заголовок, счётчики, строки, кнопки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_message_lists_records_with_show_buttons() -> None:
    bot, gateway = gateway_with_bot()
    done_task = RecordView(
        id=UUID(int=2),
        record_type=SearchRecordType.TASK,
        text="сделанная задача",
        created_at=datetime(2026, 7, 14, 10, 0, tzinfo=TZ),
        task_completed=True,
    )
    page = digest_page(
        items=(note(1, "свежая заметка"), done_task),
        total=2,
        counters=DigestCounters(
            notes=1, tasks=1, tasks_completed=1, ideas=0, decisions=0, questions=0
        ),
    )

    await gateway.send_digest(callback(902, "digest:week"), page)

    assert len(bot.sent_messages) == 1
    message = bot.sent_messages[0]
    assert message["text"] == (
        "📊 Неделя: 13.07.2026 — 15.07.2026\n"
        "📝 1 · ✅ 1 (☑️ 1 выполнено) · 💡 0 · ⚖️ 0 · ❓ 0\n\n"
        "1. 📝 Заметка · 15.07.2026 — свежая заметка\n"
        "2. ☑️ Завершённая задача · 14.07.2026 — сделанная задача"
    )
    buttons = keyboard_buttons(message)
    assert [button.text for button in buttons] == ["1", "2"]
    assert [button.callback_data for button in buttons] == [
        f"show:note:{UUID(int=1)}",
        f"show:task:{UUID(int=2)}",
    ]


@pytest.mark.asyncio
async def test_more_button_carries_the_snapshot_and_the_next_offset() -> None:
    bot, gateway = gateway_with_bot()
    items = tuple(note(number, f"заметка {number}") for number in range(1, 11))
    page = digest_page(items=items, total=25)

    await gateway.send_digest(callback(903, "digest:week"), page)

    message = bot.sent_messages[0]
    rows = message["reply_markup"].inline_keyboard
    # Номерные кнопки — рядами по 5; последний ряд — одиночная «⬇️ Ещё».
    assert [[button.text for button in row] for row in rows[:-1]] == [
        ["1", "2", "3", "4", "5"],
        ["6", "7", "8", "9", "10"],
    ]
    more_row = rows[-1]
    assert [button.text for button in more_row] == ["⬇️ Ещё"]
    expected = f"digest:more:week:10:{int(AS_OF.timestamp())}"
    assert more_row[0].callback_data == expected
    assert len(expected.encode()) <= 64


@pytest.mark.asyncio
async def test_last_page_numbers_continue_and_the_more_button_vanishes() -> None:
    bot, gateway = gateway_with_bot()
    items = tuple(note(number, f"заметка {number}") for number in range(1, 4))
    page = digest_page(items=items, total=13, offset=10)

    await gateway.send_digest(callback(904, "digest:week"), page)

    message = bot.sent_messages[0]
    assert "11. 📝 Заметка" in message["text"]
    assert "13. 📝 Заметка" in message["text"]
    buttons = keyboard_buttons(message)
    assert [button.text for button in buttons] == ["11", "12", "13"]
    assert all(button.callback_data.startswith("show:note:") for button in buttons)


@pytest.mark.asyncio
async def test_full_page_of_maximal_excerpts_fits_the_telegram_limit() -> None:
    bot, gateway = gateway_with_bot()
    items = tuple(note(number, f"слово{number:02d} " * 200) for number in range(1, 11))
    page = digest_page(items=items, total=25)

    await gateway.send_digest(callback(905, "digest:week"), page)

    message = bot.sent_messages[0]
    assert len(message["text"]) <= TELEGRAM_LIMIT
    # Штатная страница (фрагменты по 240) влезает целиком: все 10 строк на месте.
    number_buttons = keyboard_buttons(message)[:-1]
    assert [button.text for button in number_buttons] == [
        str(number) for number in range(1, 11)
    ]


@pytest.mark.asyncio
async def test_overflowing_page_shrinks_by_whole_rows_and_reoffsets_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "MAX_TELEGRAM_MESSAGE_LENGTH", 600)
    bot, gateway = gateway_with_bot()
    items = tuple(
        note(number, f"текст{number:02d}" + "х" * 90) for number in range(1, 11)
    )
    page = digest_page(items=items, total=25)

    await gateway.send_digest(callback(906, "digest:week"), page)

    message = bot.sent_messages[0]
    assert len(message["text"]) <= 600
    rows = message["reply_markup"].inline_keyboard
    number_buttons = [button for row in rows[:-1] for button in row]
    rendered = len(number_buttons)
    assert 1 <= rendered < 10
    # Строки не режутся посреди слова: последняя отрендеренная — целиком.
    assert message["text"].endswith(items[rendered - 1].text)
    assert [button.text for button in number_buttons] == [
        str(number) for number in range(1, rendered + 1)
    ]
    # Следующий offset — ФАКТИЧЕСКИ отрендеренные строки, не «+10» константой.
    more_button = rows[-1][0]
    assert more_button.callback_data == (
        f"digest:more:week:{rendered}:{int(AS_OF.timestamp())}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (DigestPeriod.WEEK, "📊 За неделю записей нет."),
        (DigestPeriod.MONTH, "📊 За месяц записей нет."),
        (DigestPeriod.HALF_YEAR, "📊 За полгода записей нет."),
        (DigestPeriod.YEAR, "📊 За год записей нет."),
    ],
)
async def test_empty_period_sends_the_honest_text_without_buttons(
    period: DigestPeriod, expected: str
) -> None:
    bot, gateway = gateway_with_bot()
    page = digest_page(items=(), total=0, period=period)

    await gateway.send_digest(callback(907, f"digest:{period.value}"), page)

    message = bot.sent_messages[0]
    assert message["text"] == expected
    assert "reply_markup" not in message


@pytest.mark.asyncio
async def test_digest_message_is_localized_in_english() -> None:
    bot, gateway = gateway_with_bot(Locale.EN)
    page = digest_page(items=(note(1, "note text"),), total=1)

    await gateway.send_digest(callback(908, "digest:week"), page)

    text = bot.sent_messages[0]["text"]
    assert text.startswith("📊 Week: 13.07.2026 — 15.07.2026\n")
    assert "done" in text
    assert "выполнено" not in text
    assert "1. 📝 Note · 15.07.2026 — note text" in text

    bot_empty, gateway_empty = gateway_with_bot(Locale.EN)
    await gateway_empty.send_digest(
        callback(909, "digest:week"), digest_page(items=(), total=0)
    )
    assert (
        bot_empty.sent_messages[0]["text"]
        == messages.CATALOG["digest.empty.week"][Locale.EN]
    )


# ---------------------------------------------------------------------------
# поллер: fresh-only side-effect, replay молчит, generic-ack исключён
# ---------------------------------------------------------------------------


class _DigestSpyGateway:
    bot_id = 1

    def __init__(self, update: TelegramUpdate) -> None:
        self._update = update
        self.calls: list[str] = []
        self.pages: list[DigestPage] = []

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

    async def send_digest_menu(self, update: TelegramUpdate) -> None:
        self.calls.append("send_digest_menu")

    async def send_digest(self, update: TelegramUpdate, result: DigestPage) -> None:
        self.calls.append("send_digest")
        self.pages.append(result)

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


def update_result(
    kind: AcknowledgementKind,
    *,
    fresh: bool,
    digest_payload: DigestPage | None = None,
) -> UpdateResult:
    return UpdateResult(
        kind,
        "1" * 32,
        "2" * 16,
        fresh=fresh,
        digest_page=digest_payload,
    )


@pytest.mark.asyncio
async def test_poller_sends_a_fresh_digest_menu_without_generic_ack() -> None:
    gateway = _DigestSpyGateway(callback(910, "digest:menu"))

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(
            update_result(AcknowledgementKind.DIGEST_MENU_SHOWN, fresh=True)
        ),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback", "send_digest_menu"]


@pytest.mark.asyncio
async def test_poller_sends_a_fresh_digest_page_without_generic_ack() -> None:
    payload = digest_page(items=(note(1, "текст"),), total=1)
    gateway = _DigestSpyGateway(callback(911, "digest:week"))

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(
            update_result(
                AcknowledgementKind.DIGEST_SHOWN, fresh=True, digest_payload=payload
            )
        ),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback", "send_digest"]
    assert gateway.pages == [payload]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [AcknowledgementKind.DIGEST_MENU_SHOWN, AcknowledgementKind.DIGEST_SHOWN],
)
async def test_poller_stays_silent_on_a_digest_replay(
    kind: AcknowledgementKind,
) -> None:
    gateway = _DigestSpyGateway(callback(912, "digest:week"))

    await LocalPoller(
        gateway,  # type: ignore[arg-type]
        _StaticProcessor(update_result(kind, fresh=False)),
        _AlwaysLock(),
    ).run_once()

    assert gateway.calls == ["answer_callback"]


@pytest.mark.asyncio
async def test_poller_requires_a_payload_for_a_fresh_digest() -> None:
    gateway = _DigestSpyGateway(callback(913, "digest:week"))

    with pytest.raises(RuntimeError):
        await LocalPoller(
            gateway,  # type: ignore[arg-type]
            _StaticProcessor(
                update_result(AcknowledgementKind.DIGEST_SHOWN, fresh=True)
            ),
            _AlwaysLock(),
        ).run_once()

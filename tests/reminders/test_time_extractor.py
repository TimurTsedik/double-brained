from datetime import UTC, datetime

import pytest

from second_brain.slices.reminders.adapters.dateparser.extractor import (
    DateparserTimeExtractor,
)

JERUSALEM = "Asia/Jerusalem"
# 13:00 в Иерусалиме (UTC+3 летом).
NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


@pytest.fixture
def extractor() -> DateparserTimeExtractor:
    return DateparserTimeExtractor()


@pytest.mark.parametrize(
    ("text", "expected_utc"),
    [
        # RU: относительная дата + часы, фраза внутри длинного текста.
        ("Позвонить в банк завтра в 10:00", datetime(2026, 7, 17, 7, 0, tzinfo=UTC)),
        # RU: относительный интервал — маркер `\d\s*ч` («2 часа») принимается.
        ("Купить билеты через 2 часа", datetime(2026, 7, 16, 12, 0, tzinfo=UTC)),
        # RU: абсолютная дата + часы.
        ("Отчёт 20 июля в 9:00", datetime(2026, 7, 20, 6, 0, tzinfo=UTC)),
        # EN: относительная дата + am/pm.
        ("Call the bank tomorrow at 10am", datetime(2026, 7, 17, 7, 0, tzinfo=UTC)),
    ],
)
def test_explicit_clock_time_yields_the_named_future_instant(
    extractor: DateparserTimeExtractor, text: str, expected_utc: datetime
) -> None:
    instant = extractor.extract_due(text, NOW, JERUSALEM)

    assert instant == expected_utc


@pytest.mark.parametrize(
    "text",
    [
        # M2: дата без времени-на-часах отвергается — иначе напомнили бы
        # в полночь или в текущий час.
        "завтра",
        "tomorrow",
        "20 июля",
        # Явное время в ПРОШЛОМ (вчерашняя дата) → None (M6).
        "вчера в 9",
        # «Сегодня в 9», когда уже 13:00, — прошло → None.
        "сегодня в 9",
        # Вовсе без времени (прескрин по цифрам).
        "Просто задача без времени",
        # Голый час принимается ТОЛЬКО в конце текста: «в N» посреди фразы —
        # обычный текст, не время. Лучше пропущенное напоминание, чем ложное.
        "увеличить в 3 раза",
        "в 12 квартире холодно",
        "meet at 5 with John",
        # Не час суток (25 > 23).
        "в 25",
    ],
)
def test_date_only_past_or_timeless_text_yields_none(
    extractor: DateparserTimeExtractor, text: str
) -> None:
    assert extractor.extract_due(text, NOW, JERUSALEM) is None


def test_m6_bare_clock_already_past_today_rolls_to_tomorrow(
    extractor: DateparserTimeExtractor,
) -> None:
    # M6: now = 10:00 локального, «в 9» уже прошло СЕГОДНЯ → завтра 09:00
    # (не None и не сегодня-в-прошлом): напоминание всегда в будущее.
    now = datetime(2026, 7, 16, 7, 0, tzinfo=UTC)  # 10:00 в Иерусалиме

    instant = extractor.extract_due("Позвонить в 9", now, JERUSALEM)

    assert instant == datetime(2026, 7, 17, 6, 0, tzinfo=UTC)  # завтра 09:00 local


def test_bare_hour_at_end_of_text_is_accepted_in_english_too(
    extractor: DateparserTimeExtractor,
) -> None:
    # Конец текста — единственное место, где голое «at H» читается как время.
    # now = 13:00 локального → 5:00 уже прошло → завтра 05:00 (+03:00) = 02:00 UTC.
    instant = extractor.extract_due("Call the bank at 5", NOW, JERUSALEM)

    assert instant == datetime(2026, 7, 17, 2, 0, tzinfo=UTC)


def test_m3_same_clock_time_in_different_space_timezones_differs_in_utc(
    extractor: DateparserTimeExtractor,
) -> None:
    text = "Позвонить в банк в 10:00"

    jerusalem = extractor.extract_due(text, NOW, JERUSALEM)
    new_york = extractor.extract_due(text, NOW, "America/New_York")

    # Иерусалим: 13:00 local → 10:00 уже прошло → завтра 10:00 (+03:00).
    assert jerusalem == datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    # Нью-Йорк: 06:00 local → сегодня 10:00 (−04:00 EDT).
    assert new_york == datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
    assert jerusalem != new_york

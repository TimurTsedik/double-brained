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


def test_bug_a_clock_two_minutes_ahead_stays_today(
    extractor: DateparserTimeExtractor,
) -> None:
    # BUG A (прод): «позвонить Ави в 11:53» в 11:51 локального создало
    # напоминание на ЗАВТРА 11:53. Должно быть СЕГОДНЯ: 11:53 ещё впереди.
    now = datetime(2026, 7, 16, 8, 51, tzinfo=UTC)  # 11:51 в Иерусалиме (+03)

    instant = extractor.extract_due("позвонить Ави в 11:53", now, JERUSALEM)

    assert instant == datetime(2026, 7, 16, 8, 53, tzinfo=UTC)  # сегодня 11:53


def test_bug_b_dash_clock_is_time_of_day_not_year(
    extractor: DateparserTimeExtractor,
) -> None:
    # BUG B (прод): «в 11-52» распарсилось как ГОД 2052. Тире-формат часов —
    # обычная русская запись времени, нормализуется в 11:52.
    now = datetime(2026, 7, 16, 8, 50, tzinfo=UTC)  # 11:50 в Иерусалиме

    instant = extractor.extract_due("позвонить Ави в 11-52", now, JERUSALEM)

    assert instant == datetime(2026, 7, 16, 8, 52, tzinfo=UTC)  # сегодня 11:52


def test_bug_b_sanity_cap_rejects_far_future_instants(
    extractor: DateparserTimeExtractor,
) -> None:
    # SANITY CAP: любой результат дальше 366 дней от now — абсурдный
    # мис-парс как класс → None (страховка от «2052 года»).
    instant = extractor.extract_due("встреча 20 июля 2028 в 9:00", NOW, JERUSALEM)

    assert instant is None


def test_bug_c_relative_minutes_are_recognized(
    extractor: DateparserTimeExtractor,
) -> None:
    # BUG C (прод): «через 5 минут» не дало ничего — маркер знал часы, но не
    # минуты.
    instant = extractor.extract_due(
        "Напомни мне позвонить Ави через 5 минут", NOW, JERUSALEM
    )

    assert instant == datetime(2026, 7, 16, 10, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected_utc"),
    [
        # Прод-находка владельца: «через 1 минуту» работало, «через минуту» — нет
        # (прескрин требовал цифру, маркер — цифру перед словом времени).
        ("позвонить через минуту", datetime(2026, 7, 16, 10, 1, tzinfo=UTC)),
        ("встретиться через час", datetime(2026, 7, 16, 11, 0, tzinfo=UTC)),
        ("выйти через полчаса", datetime(2026, 7, 16, 10, 30, tzinfo=UTC)),
        ("позвонить через пару минут", datetime(2026, 7, 16, 10, 2, tzinfo=UTC)),
        ("созвон через пару часов", datetime(2026, 7, 16, 12, 0, tzinfo=UTC)),
        ("call mom in a minute", datetime(2026, 7, 16, 10, 1, tzinfo=UTC)),
        ("check the oven in an hour", datetime(2026, 7, 16, 11, 0, tzinfo=UTC)),
        # dateparser это НЕ парсит (search_dates видит лишь «an hour» → +1 час,
        # что неверно) — считается детерминированно: +30 минут.
        ("leave in half an hour", datetime(2026, 7, 16, 10, 30, tzinfo=UTC)),
    ],
)
def test_wordy_relative_time_without_digits_is_recognized(
    extractor: DateparserTimeExtractor, text: str, expected_utc: datetime
) -> None:
    instant = extractor.extract_due(text, NOW, JERUSALEM)

    assert instant == expected_utc


@pytest.mark.parametrize(
    ("text", "expected_utc"),
    [
        # Прод-находка владельца: число СЛОВОМ («через две минуты») отсекалось
        # прескрином и маркером. dateparser считает такие верно — пропускаем.
        ("позвонить через две минуты", datetime(2026, 7, 16, 10, 2, tzinfo=UTC)),
        ("напомни через три минуты сделать", datetime(2026, 7, 16, 10, 3, tzinfo=UTC)),
        ("через пять минут", datetime(2026, 7, 16, 10, 5, tzinfo=UTC)),
        ("созвон через два часа", datetime(2026, 7, 16, 12, 0, tzinfo=UTC)),
        ("через десять минут", datetime(2026, 7, 16, 10, 10, tzinfo=UTC)),
        ("выйти через полтора часа", datetime(2026, 7, 16, 11, 30, tzinfo=UTC)),
        ("call the bank in two minutes", datetime(2026, 7, 16, 10, 2, tzinfo=UTC)),
        ("remind me in three hours", datetime(2026, 7, 16, 13, 0, tzinfo=UTC)),
        ("ping in five minutes", datetime(2026, 7, 16, 10, 5, tzinfo=UTC)),
    ],
)
def test_wordy_number_relative_time_is_recognized(
    extractor: DateparserTimeExtractor, text: str, expected_utc: datetime
) -> None:
    instant = extractor.extract_due(text, NOW, JERUSALEM)

    assert instant == expected_utc


@pytest.mark.parametrize(
    "text",
    [
        # «через» без слова времени — обычный текст, не напоминание.
        "сходить через дорогу",
        # «через неделю» — дата без времени-суток → None по правилу M2.
        "вернуться через неделю",
        # Число словом БЕЗ единицы времени / не время-суток → не напоминание.
        "увеличить в три раза",
        "in three days",
    ],
)
def test_wordy_relative_without_clock_meaning_yields_none(
    extractor: DateparserTimeExtractor, text: str
) -> None:
    assert extractor.extract_due(text, NOW, JERUSALEM) is None


def test_clock_one_minute_before_midnight_stays_today(
    extractor: DateparserTimeExtractor,
) -> None:
    # Пин детерминированной сборки: 23:59 за минуту до полуночи — ещё СЕГОДНЯ.
    now = datetime(2026, 7, 16, 20, 58, tzinfo=UTC)  # 23:58 в Иерусалиме

    instant = extractor.extract_due("в 23:59", now, JERUSALEM)

    assert instant == datetime(2026, 7, 16, 20, 59, tzinfo=UTC)  # сегодня 23:59


def test_clock_already_past_rolls_to_tomorrow_after_midnight(
    extractor: DateparserTimeExtractor,
) -> None:
    # Пин детерминированной сборки: «в 00:30» в 23:00 → ЗАВТРА 00:30.
    now = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)  # 23:00 в Иерусалиме

    instant = extractor.extract_due("в 00:30", now, JERUSALEM)

    # 17.07 00:30 (+03:00) = 16.07 21:30 UTC.
    assert instant == datetime(2026, 7, 16, 21, 30, tzinfo=UTC)


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

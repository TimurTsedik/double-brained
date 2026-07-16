import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateparser import parse as parse_date
from dateparser.search import search_dates

# M2: принимаем совпадение ТОЛЬКО при явном маркере времени-суток в найденной
# подстроке. Дата-без-времени («завтра», «20 июля») даёт None, иначе напомнили бы
# в полночь/в текущий час. Регекс — дословно из спеки + минуты (hotfix BUG C:
# «через 5 минут» раньше отбрасывалось — маркер знал часы, но не минуты).
_TIME_MARKER = re.compile(
    r"\d{1,2}[:.]\d{2}"
    r"|\bв\s*\d{1,2}\b"
    r"|\bat\s*\d"
    r"|\d\s*(?:am|pm|утра|вечера|дня|ночи|минут|мин|minutes|min|ч|h)"
    # Словесные относительные БЕЗ цифр («через минуту», "in an hour") — прод-
    # находка: «через 1 минуту» работало, «через минуту» — нет. Список закрыт:
    # «через неделю/дорогу» сюда не входят и остаются date-only/не-временем.
    r"|\bчерез\s+(?:минуту|полчаса|час\b|пару\s+(?:минут|часов))"
    r"|\bin\s+(?:a\s+minute|an\s+hour|half\s+an\s+hour)\b",
    re.IGNORECASE,
)

# Те же словесные формы для прескрина и детерминированного запасного пути.
# dateparser сам разбирает все, КРОМЕ "in half an hour" (search_dates видит лишь
# "an hour" → +1 час, т.е. неверно) — такие считаем сами по таблице сдвигов.
_WORDY_RELATIVE = re.compile(
    r"\bчерез\s+(?:(?P<minute>минуту)|(?P<half_hour>полчаса)|(?P<hour>час)\b"
    r"|пару\s+(?:(?P<two_minutes>минут)|(?P<two_hours>часов)))"
    r"|\bin\s+(?:(?P<en_minute>a\s+minute)|(?P<en_half_hour>half\s+an\s+hour)"
    r"|(?P<en_hour>an\s+hour))\b",
    re.IGNORECASE,
)

_WORDY_DELTAS = {
    "minute": timedelta(minutes=1),
    "en_minute": timedelta(minutes=1),
    "half_hour": timedelta(minutes=30),
    "en_half_hour": timedelta(minutes=30),
    "hour": timedelta(hours=1),
    "en_hour": timedelta(hours=1),
    "two_minutes": timedelta(minutes=2),
    "two_hours": timedelta(hours=2),
}

# Hotfix BUG B: русская тире-запись часов «в 11-52» dateparser читал как ГОД
# 2052. Нормализуем в «11:52» ДО парсинга — только при валидных часах/минутах.
_DASH_CLOCK = re.compile(r"(?<=\bв\s)(\d{1,2})-(\d{2})\b", re.IGNORECASE)

# Hotfix BUG A: dateparser с PREFER_DATES_FROM='future' решает «сегодня или
# завтра» с точностью до ДНЯ и катит время-без-даты («в 11:53» в 11:51) на
# завтра. Часы-минуты из совпадения извлекаем сами и мгновение строим сами;
# dateparser остаётся только для поиска фразы и явных дат/относительных.
_CLOCK = re.compile(
    r"\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>am|pm)\b"
    r"|\b(?P<h24>\d{1,2})[:.](?P<m24>\d{2})\b",
    re.IGNORECASE,
)

# Служебные предлоги вокруг часов: не считаются «явной датой» в остатке фразы.
_CONNECTIVES = re.compile(r"\b(?:в|во|at)\b", re.IGNORECASE)

# SANITY CAP (hotfix BUG B, вторая линия): мгновение дальше 366 дней от now —
# абсурдный мис-парс как класс («год 2052») → отвергается.
_MAX_FUTURE_DRIFT = timedelta(days=366)

# Голый час «в 9» / «at 9», который search_dates не распознаёт как время: берём
# число и строим HH:00 сами (M6 — ближайшее будущее вхождение). Осознанно
# КОНСЕРВАТИВНО: принимается ТОЛЬКО в самом КОНЦЕ текста (дальше — лишь пробелы/
# знаки препинания), иначе «увеличить в 3 раза» / «meet at 5 with John» дали бы
# ложное напоминание. Лучше пропущенное напоминание, чем неверное.
_BARE_HOUR = re.compile(r"\b(?:в|at)\s*(\d{1,2})\s*[.!?…)]*\s*$", re.IGNORECASE)

# Копеечный прескрин: длинный заголовок без времени не гоняем через парсер.
_MAX_TEXT_LENGTH = 500

_SETTINGS_LANGUAGES = ["ru", "en"]


class DateparserTimeExtractor:
    """TimeExtractor over ``dateparser``. Deterministic given ``now`` + ``tz``."""

    def extract_due(self, text: str, now: datetime, tz: str) -> datetime | None:
        # Прескрин: слишком длинно, либо нет ни цифр, ни словесной относительной
        # формы («через минуту») → времени быть не может.
        if len(text) > _MAX_TEXT_LENGTH:
            return None
        if not any(ch.isdigit() for ch in text) and not _WORDY_RELATIVE.search(text):
            return None

        text = _normalize_dash_clock(text)
        zone = ZoneInfo(tz)
        now_local = now.astimezone(zone)
        # M3: RELATIVE_BASE — локальные «часы на стене» пространства; TIMEZONE
        # говорит парсеру зону; DST учитывает ZoneInfo при обратной конвертации.
        settings = {
            "RELATIVE_BASE": now_local.replace(tzinfo=None),
            "TIMEZONE": tz,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        }

        found = search_dates(text, languages=_SETTINGS_LANGUAGES, settings=settings)
        marked = [match for match in (found or []) if _TIME_MARKER.search(match[0])]
        for matched_text, _ in marked:
            instant = _instant_from_match(matched_text, now_local, zone, settings)
            if instant is None:
                continue
            if instant > now and instant - now <= _MAX_FUTURE_DRIFT:
                return instant.astimezone(UTC)

        if marked:
            # Явное время нашлось, но всё в прошлом (напр. «вчера в 9») или за
            # sanity cap → None. НЕ падаем в fallback: дата задана намеренно (M6).
            return None

        # Словесная относительная форма, которую dateparser не разобрал
        # ("in half an hour"): сдвиг от now по таблице — детерминированно.
        wordy = _wordy_relative_instant(text, now)
        if wordy is not None:
            return wordy

        # Голый час без даты, который search_dates пропустил (напр. «в 9»):
        # ближайшее будущее HH:00 в зоне пространства (M6).
        return _bare_hour_instant(text, now_local, now)


def _normalize_dash_clock(text: str) -> str:
    """«в 11-52» → «в 11:52» — только при валидных часах (0-23) и минутах."""

    def repl(match: re.Match[str]) -> str:
        if int(match.group(1)) <= 23 and int(match.group(2)) <= 59:
            return f"{match.group(1)}:{match.group(2)}"
        return match.group(0)

    return _DASH_CLOCK.sub(repl, text)


def _instant_from_match(
    matched_text: str,
    now_local: datetime,
    zone: ZoneInfo,
    settings: dict[str, object],
) -> datetime | None:
    """Мгновение из совпадения: часы строим сами, dateparser — только даты.

    Совпадение с явными часами-минутами (HH:MM или H am/pm) собирается
    детерминированно (см. ``_clock_instant``). Остальное (относительные
    «через 2 часа», «вчера в 9») — переразбор dateparser'ом, как раньше:
    там RELATIVE_BASE даёт предсказуемый результат.
    """
    clock = _CLOCK.search(matched_text)
    if clock is not None:
        parts = _clock_parts(clock)
        if parts is not None:
            hour, minute = parts
            return _clock_instant(
                matched_text, clock, hour, minute, now_local, zone, settings
            )
    instant = parse_date(matched_text, languages=_SETTINGS_LANGUAGES, settings=settings)
    if instant is None:
        return None
    return _localize(instant, zone)


def _clock_parts(clock: re.Match[str]) -> tuple[int, int] | None:
    """(час, минута) из совпадения ``_CLOCK``; None при невалидных значениях."""
    if clock.group("ampm") is not None:
        hour = int(clock.group("h"))
        minute = int(clock.group("m") or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        hour = hour % 12 + (12 if clock.group("ampm").lower() == "pm" else 0)
        return hour, minute
    hour = int(clock.group("h24"))
    minute = int(clock.group("m24"))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _clock_instant(
    matched_text: str,
    clock: re.Match[str],
    hour: int,
    minute: int,
    now_local: datetime,
    zone: ZoneInfo,
    settings: dict[str, object],
) -> datetime | None:
    """Детерминированная сборка мгновения для совпадения с часами (hotfix BUG A).

    Если в фразе кроме часов есть явная дата («завтра», «20 июля») — день берём
    из dateparser, время ставим своё. Если фраза — только время («в 11:53») —
    сегодня в HH:MM, а если уже прошло, то завтра. Никакого day-roll от
    dateparser: сегодня-или-завтра решаем сами по часам.
    """
    rest = matched_text[: clock.start()] + matched_text[clock.end() :]
    rest = _CONNECTIVES.sub(" ", rest)
    has_explicit_date = any(ch.isalnum() for ch in rest)
    if has_explicit_date:
        parsed = parse_date(
            matched_text, languages=_SETTINGS_LANGUAGES, settings=settings
        )
        if parsed is None:
            return None
        day = _localize(parsed, zone).astimezone(zone).date()
        return datetime.combine(day, time(hour, minute), tzinfo=zone)
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def _wordy_relative_instant(text: str, now: datetime) -> datetime | None:
    match = _WORDY_RELATIVE.search(text)
    if match is None:
        return None
    delta = _WORDY_DELTAS[str(match.lastgroup)]
    return (now + delta).astimezone(UTC)


def _bare_hour_instant(
    text: str, now_local: datetime, now: datetime
) -> datetime | None:
    match = _BARE_HOUR.search(text)
    if match is None:
        return None
    hour = int(match.group(1))
    if hour > 23:
        return None
    candidate = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _localize(instant: datetime, zone: ZoneInfo) -> datetime:
    if instant.tzinfo is None:
        return instant.replace(tzinfo=zone)
    return instant

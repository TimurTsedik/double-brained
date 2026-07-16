import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateparser import parse as parse_date
from dateparser.search import search_dates

# M2: принимаем совпадение ТОЛЬКО при явном маркере времени-суток в найденной
# подстроке. Дата-без-времени («завтра», «20 июля») даёт None, иначе напомнили бы
# в полночь/в текущий час. Регекс — дословно из спеки.
_TIME_MARKER = re.compile(
    r"\d{1,2}[:.]\d{2}"
    r"|\bв\s*\d{1,2}\b"
    r"|\bat\s*\d"
    r"|\d\s*(?:am|pm|утра|вечера|дня|ночи|ч|h)",
    re.IGNORECASE,
)

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
        # Прескрин: нет цифр или слишком длинно → времени быть не может.
        if len(text) > _MAX_TEXT_LENGTH or not any(ch.isdigit() for ch in text):
            return None

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
            # Переразбор найденной подстроки точнее, чем результат search_dates
            # (напр. «tomorrow at 10am» → 10:00, а не час базы).
            instant = parse_date(
                matched_text, languages=_SETTINGS_LANGUAGES, settings=settings
            )
            if instant is None:
                continue
            instant = _localize(instant, zone)
            if instant > now:
                return instant.astimezone(UTC)

        if marked:
            # Явное время нашлось, но всё в прошлом (напр. «вчера в 9») → None.
            # НЕ падаем в fallback: прошлая дата задана намеренно (M6).
            return None

        # Голый час без даты, который search_dates пропустил (напр. «в 9»):
        # ближайшее будущее HH:00 в зоне пространства (M6).
        return _bare_hour_instant(text, now_local, now)


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

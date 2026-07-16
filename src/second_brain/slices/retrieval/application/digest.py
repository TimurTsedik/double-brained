from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import DigestPage
from second_brain.slices.retrieval.domain.entities import DigestPeriod
from second_brain.slices.retrieval.ports.repositories import DigestStore

DIGEST_PAGE_SIZE = 10


def digest_period_start(
    period: DigestPeriod, as_of: datetime, timezone: ZoneInfo
) -> datetime:
    """Календарное начало периода в поясе пространства.

    Стартовая дата считается ЛОКАЛЬНЫМ календарём (понедельник — фиксированно,
    не locale; месяц — с 1-го; полугодие — с 1 января / 1 июля; год — с
    1 января), полночь конструируется в ZoneInfo и только затем сравнивается
    как UTC-момент — никакой арифметики «минус N дней» по timestamp'у.
    """
    local = as_of.astimezone(timezone)
    if period is DigestPeriod.WEEK:
        start_date = local.date() - timedelta(days=local.weekday())
    elif period is DigestPeriod.MONTH:
        start_date = date(local.year, local.month, 1)
    elif period is DigestPeriod.HALF_YEAR:
        start_date = date(local.year, 1 if local.month <= 6 else 7, 1)
    else:
        start_date = date(local.year, 1, 1)
    return datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone)


class BuildDigest:
    """Собирает страницу сводки: счётчики + записи ОДНОГО снимка as_of.

    Обе выборки ограничены `period_start <= created_at <= as_of`, поэтому
    записи, созданные между страницами, не сдвигают список и не расходятся
    со счётчиками. Даты результата — в поясе пространства.
    """

    def __init__(self, store: DigestStore) -> None:
        self._store = store

    async def read_page(
        self,
        access_context: AccessContext,
        period: DigestPeriod,
        offset: int,
        as_of: datetime,
        timezone: ZoneInfo,
    ) -> DigestPage:
        start = digest_period_start(period, as_of, timezone)
        counters = await self._store.count_records(access_context, start, as_of)
        items = await self._store.read_page(
            access_context, start, as_of, offset, DIGEST_PAGE_SIZE
        )
        return DigestPage(
            period=period,
            period_start=start,
            as_of=as_of.astimezone(timezone),
            offset=offset,
            total=counters.total,
            counters=counters,
            items=tuple(
                replace(item, created_at=item.created_at.astimezone(timezone))
                for item in items
            ),
        )

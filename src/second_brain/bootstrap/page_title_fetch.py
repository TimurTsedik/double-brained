"""Шаг воркера: подтянуть <title> для pending-строк page_titles.

Claimed-work по образцу ReminderDeliveryStep, но HTTP-фетч идёт СТРОГО ВНЕ
транзакции: claim ОДНОЙ строки своей транзакцией (FOR UPDATE SKIP LOCKED,
attempt_count++, next_attempt_at-бэкофф как lease), затем сеть без
блокировок, затем итог отдельной транзакцией — fetched (title/NULL) или, при
исчерпании бюджета попыток, failed. TITLE_FETCH_ENABLED=off → шаг не клеймит.
"""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.weblinks.adapters.persistence.repository import (
    PostgresPageTitleQueue,
)
from second_brain.slices.weblinks.application.contracts import ClaimedPageTitle
from second_brain.slices.weblinks.ports.title_fetcher import (
    TitleFetcher,
    TitleFetchOutcome,
)


class PageTitleFetchStep:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher: TitleFetcher,
        *,
        enabled: bool,
        max_attempts: int,
        retry_backoff: timedelta,
    ) -> None:
        self._session_factory = session_factory
        self._fetcher = fetcher
        self._enabled = enabled
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff

    async def process_once(self, access_context: AccessContext, now: datetime) -> bool:
        if not self._enabled:
            return False
        # Догоняем все созревшие — по одному claimed-unit на транзакцию.
        worked = False
        while True:
            claimed = await self._claim(access_context, now)
            if claimed is None:
                return worked
            worked = True
            outcome = await self._fetch(claimed.original_url)
            await self._record(access_context, claimed, outcome, now)

    async def _fetch(self, url: str) -> TitleFetchOutcome:
        # Контракт фетчера — «никогда не кидает», но чужая реализация может:
        # исключение сворачивается в мягкий сбой, строка ждёт свой бэкофф.
        try:
            return await self._fetcher.fetch_title(url)
        except Exception:
            return TitleFetchOutcome(ok=False)

    async def _claim(
        self, access_context: AccessContext, now: datetime
    ) -> ClaimedPageTitle | None:
        async with self._session_factory() as session, session.begin():
            return await PostgresPageTitleQueue(session).claim_pending(
                access_context,
                now,
                max_attempts=self._max_attempts,
                retry_backoff=self._retry_backoff,
            )

    async def _record(
        self,
        access_context: AccessContext,
        claimed: ClaimedPageTitle,
        outcome: TitleFetchOutcome,
        now: datetime,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            queue = PostgresPageTitleQueue(session)
            if outcome.ok:
                await queue.mark_fetched(
                    access_context, claimed.page_title_id, outcome.title, now
                )
                return
            await queue.record_fetch_failure(
                access_context,
                claimed.page_title_id,
                now,
                max_attempts=self._max_attempts,
            )

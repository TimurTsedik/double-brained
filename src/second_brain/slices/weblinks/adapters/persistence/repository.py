"""Postgres-адаптеры weblinks: запись sidecar-ссылок, чтение для показа и
claimed-work очередь титулов (по образцу reminder-delivery)."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.weblinks.adapters.normalization import normalize_url
from second_brain.slices.weblinks.adapters.persistence.models import (
    PageTitleModel,
    RecordUrlModel,
)
from second_brain.slices.weblinks.application.contracts import (
    ClaimedPageTitle,
    RecordLinkView,
    SaveRecordLinksCommand,
)
from second_brain.slices.weblinks.domain.entities import (
    PageTitleStatus,
    WeblinkRecordKind,
)


class PostgresWeblinkWriter:
    """Ссылки записи через транзакцию вызывающего (та же, что создала запись)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_links(self, command: SaveRecordLinksCommand) -> None:
        # Sidecar в ТОМ ЖЕ коммите, что запись: упорядоченные пары «слово →
        # адрес» + идемпотентная постановка URL в очередь титулов. Текст записи
        # не трогается.
        await _set_user_space_scope(self._session, command.access_context)
        for position, entry in enumerate(command.entries):
            self._session.add(
                RecordUrlModel(
                    id=uuid4(),
                    user_space_id=command.access_context.user_space_id,
                    record_kind=command.record_kind,
                    record_id=command.record_id,
                    position=position,
                    label=entry.label,
                    url=entry.url,
                    created_at=command.created_at,
                    trace_id=command.trace_id,
                )
            )
        seen: set[str] = set()
        for entry in command.entries:
            normalized = normalize_url(entry.url)
            # Неканонизируемый URL (userinfo/битый порт/не-http) в очередь не
            # ставится: фетчер его всё равно отвергнет, а слот дедупа он
            # отравил бы для нормальной формы той же страницы.
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            # Конфликт по (user_space_id, normalized_url) гасится: страница уже
            # в очереди/кэше — original_url остаётся как прислан ПЕРВЫМ.
            await self._session.execute(
                insert(PageTitleModel)
                .values(
                    id=uuid4(),
                    user_space_id=command.access_context.user_space_id,
                    original_url=entry.url,
                    normalized_url=normalized,
                    title=None,
                    status=PageTitleStatus.PENDING.value,
                    attempt_count=0,
                    next_attempt_at=command.created_at,
                    fetched_at=None,
                    created_at=command.created_at,
                    updated_at=command.created_at,
                    trace_id=command.trace_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["user_space_id", "normalized_url"]
                )
            )
        await self._session.flush()

    async def replace_links(self, command: SaveRecordLinksCommand) -> None:
        # Правка записи (S3): ссылки отражают ТЕКУЩИЙ текст — прежний набор
        # пар записи снимается целиком, новый пишется тем же save_links (в т.ч.
        # пустой: текст без ссылок → блока ссылок больше нет). Кэш/очередь
        # page_titles не чистим: титул страницы валиден независимо от того,
        # какая запись на неё ссылается.
        await _set_user_space_scope(self._session, command.access_context)
        await self._session.execute(
            delete(RecordUrlModel).where(
                RecordUrlModel.user_space_id == command.access_context.user_space_id,
                RecordUrlModel.record_kind == command.record_kind,
                RecordUrlModel.record_id == command.record_id,
            )
        )
        await self.save_links(command)

    async def links_for_record(
        self,
        access_context: AccessContext,
        record_kind: WeblinkRecordKind,
        record_id: UUID,
    ) -> tuple[RecordLinkView, ...]:
        # Показ: ссылки в порядке position; title подтягивается только для
        # fetched-строк своего пространства — pending/failed остаются «голыми».
        await _set_user_space_scope(self._session, access_context)
        rows = (
            await self._session.scalars(
                select(RecordUrlModel)
                .where(
                    RecordUrlModel.user_space_id == access_context.user_space_id,
                    RecordUrlModel.record_kind == record_kind,
                    RecordUrlModel.record_id == record_id,
                )
                .order_by(RecordUrlModel.position)
            )
        ).all()
        if not rows:
            return ()
        normalized_by_url = {
            row.url: normalized
            for row in rows
            if (normalized := normalize_url(row.url)) is not None
        }
        titles: dict[str, str | None] = {}
        if normalized_by_url:
            title_rows = (
                await self._session.execute(
                    select(PageTitleModel.normalized_url, PageTitleModel.title).where(
                        PageTitleModel.user_space_id == access_context.user_space_id,
                        PageTitleModel.normalized_url.in_(
                            set(normalized_by_url.values())
                        ),
                        PageTitleModel.status == PageTitleStatus.FETCHED,
                        PageTitleModel.title.is_not(None),
                    )
                )
            ).all()
            titles = {normalized: title for normalized, title in title_rows}
        return tuple(
            RecordLinkView(
                label=row.label,
                url=row.url,
                title=(
                    titles.get(normalized)
                    if (normalized := normalized_by_url.get(row.url)) is not None
                    else None
                ),
            )
            for row in rows
        )


class PostgresPageTitleQueue:
    """Claimed-work очередь титулов через транзакцию вызывающего.

    Claim ОДНОЙ pending-строки под FOR UPDATE SKIP LOCKED с attempt_count++ и
    бэкоффом next_attempt_at ВПЕРЁД — это lease: упавший между claim'ом и
    итогом воркер не зациклит строку горячо, она созреет к следующей попытке.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_pending(
        self,
        access_context: AccessContext,
        now: datetime,
        *,
        max_attempts: int,
        retry_backoff: timedelta,
    ) -> ClaimedPageTitle | None:
        await _set_user_space_scope(self._session, access_context)
        while True:
            claimed = await self._session.scalar(
                select(PageTitleModel)
                .where(
                    PageTitleModel.user_space_id == access_context.user_space_id,
                    PageTitleModel.status == PageTitleStatus.PENDING,
                    or_(
                        PageTitleModel.next_attempt_at.is_(None),
                        PageTitleModel.next_attempt_at <= now,
                    ),
                )
                .order_by(PageTitleModel.created_at, PageTitleModel.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if claimed is None:
                return None
            claimed.attempt_count += 1
            claimed.updated_at = now
            if claimed.attempt_count > max_attempts:
                # Хвост после падения между claim'ом и итогом: бюджет уже
                # выбран — failed, берём следующую строку.
                claimed.status = PageTitleStatus.FAILED
                await self._session.flush()
                continue
            claimed.next_attempt_at = now + claimed.attempt_count * retry_backoff
            await self._session.flush()
            return ClaimedPageTitle(
                page_title_id=claimed.id,
                original_url=claimed.original_url,
                attempt_count=claimed.attempt_count,
                trace_id=claimed.trace_id,
            )

    async def mark_fetched(
        self,
        access_context: AccessContext,
        page_title_id: UUID,
        title: str | None,
        now: datetime,
    ) -> None:
        # Итог честного чтения страницы: fetched даже при title=None (у
        # страницы нет <title> — ретраить нечего). Только pending → fetched.
        await _set_user_space_scope(self._session, access_context)
        fetched = await self._session.scalar(
            select(PageTitleModel)
            .where(
                PageTitleModel.id == page_title_id,
                PageTitleModel.user_space_id == access_context.user_space_id,
                PageTitleModel.status == PageTitleStatus.PENDING,
            )
            .with_for_update()
        )
        if fetched is None:
            return
        fetched.status = PageTitleStatus.FETCHED
        fetched.title = title
        fetched.fetched_at = now
        fetched.updated_at = now
        await self._session.flush()

    async def record_fetch_failure(
        self,
        access_context: AccessContext,
        page_title_id: UUID,
        now: datetime,
        *,
        max_attempts: int,
    ) -> None:
        # Сбой фетча: попытка уже учтена claim'ом (lease), здесь только
        # решение «потолок → failed», иначе строка ждёт свой бэкофф pending'ом.
        await _set_user_space_scope(self._session, access_context)
        failed = await self._session.scalar(
            select(PageTitleModel)
            .where(
                PageTitleModel.id == page_title_id,
                PageTitleModel.user_space_id == access_context.user_space_id,
                PageTitleModel.status == PageTitleStatus.PENDING,
            )
            .with_for_update()
        )
        if failed is None:
            return
        if failed.attempt_count >= max_attempts:
            failed.status = PageTitleStatus.FAILED
        failed.updated_at = now
        await self._session.flush()


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )

"""Postgres-очередь webhook-апдейтов (telegram_update_inbox).

«Очередь и истина — Postgres» (эпик API-1 §1): webhook-роут кладёт сырой
апдейт идемпотентным INSERT'ом, inbox-шаг воркера обрабатывает строки СТРОГО
в порядке update_id внутри бота. Claim — lease по образцу page_titles:
attempt_count++ и next_attempt_at вперёд ещё ДО обработки, чтобы упавший
между claim'ом и итогом воркер не зациклил строку горячо. Пока головная
строка бота pending и не созрела — хвост не выдаётся (строгий порядок важнее
throughput); failed-голова хвост не блокирует.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.slices.identity.adapters.persistence.models import (
    TelegramUpdateInbox,
)
from second_brain.slices.identity.domain.entities import TelegramInboxStatus


@dataclass(frozen=True)
class ClaimedInboxUpdate:
    """Строка INBOX, взятая в работу (payload — PII, вне repr/логов)."""

    id: UUID
    bot_id: int
    update_id: int
    payload: dict[str, object] = field(repr=False)
    attempt_count: int
    trace_id: str


@dataclass(frozen=True)
class TelegramInboxHealth:
    """Снимок глубины INBOX для статуса/монитора (B4 растит его дальше)."""

    pending_count: int
    failed_count: int
    # Возраст головной pending-строки, секунды; None — pending-строк нет.
    head_age_seconds: float | None


class PostgresTelegramInboxQueue:
    """Очередь INBOX через транзакцию вызывающего (как PostgresPageTitleQueue)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        bot_id: int,
        update_id: int,
        payload: dict[str, object],
        received_at: datetime,
        trace_id: str,
    ) -> bool:
        """Идемпотентная постановка: конфликт (bot_id, update_id) гасит дубль.

        Возвращает True, если строка вставлена, False — дубль ретрая Telegram.
        """
        inserted_id = await self._session.scalar(
            insert(TelegramUpdateInbox)
            .values(
                id=uuid4(),
                bot_id=bot_id,
                update_id=update_id,
                payload=payload,
                received_at=received_at,
                status=TelegramInboxStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=None,
                trace_id=trace_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    TelegramUpdateInbox.bot_id,
                    TelegramUpdateInbox.update_id,
                ]
            )
            .returning(TelegramUpdateInbox.id)
        )
        return inserted_id is not None

    async def claim_head(
        self,
        now: datetime,
        *,
        bot_id: int,
        max_attempts: int,
        retry_backoff: timedelta,
    ) -> ClaimedInboxUpdate | None:
        """Взять в работу СОЗРЕВШУЮ головную строку бота.

        Бот всегда известен вызывающему (id — префикс токена), поэтому запрос
        ходит только по строкам этого бота и ложится на индекс
        (bot_id, status, update_id) — без скана всей таблицы с done-историей.
        Голова = min update_id среди pending. Незрелая или занятая другим
        воркером голова блокирует ВЕСЬ хвост. Строка с выбранным бюджетом
        попыток (крах между claim'ом и итогом) добивается в failed, и голова
        пересчитывается.
        """
        while True:
            head_update_id = await self._session.scalar(
                select(func.min(TelegramUpdateInbox.update_id)).where(
                    TelegramUpdateInbox.bot_id == bot_id,
                    TelegramUpdateInbox.status == TelegramInboxStatus.PENDING,
                )
            )
            if head_update_id is None:
                return None
            # Лочится ИМЕННО голова: ORDER BY + SKIP LOCKED здесь нельзя —
            # занятая голова отдала бы следующую строку и сломала порядок.
            claimed = await self._session.scalar(
                select(TelegramUpdateInbox)
                .where(
                    TelegramUpdateInbox.bot_id == bot_id,
                    TelegramUpdateInbox.update_id == head_update_id,
                    TelegramUpdateInbox.status == TelegramInboxStatus.PENDING,
                    or_(
                        TelegramUpdateInbox.next_attempt_at.is_(None),
                        TelegramUpdateInbox.next_attempt_at <= now,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if claimed is None:
                # Голова не созрела (бэкофф) или занята — хвост бота ждёт.
                return None
            claimed.attempt_count += 1
            if claimed.attempt_count > max_attempts:
                # Хвост краха: бюджет уже выбран — failed, голова пересчитывается.
                claimed.status = TelegramInboxStatus.FAILED
                await self._session.flush()
                continue
            claimed.next_attempt_at = now + claimed.attempt_count * retry_backoff
            await self._session.flush()
            return ClaimedInboxUpdate(
                id=claimed.id,
                bot_id=claimed.bot_id,
                update_id=claimed.update_id,
                payload=claimed.payload,
                attempt_count=claimed.attempt_count,
                trace_id=claimed.trace_id,
            )

    async def mark_done(self, inbox_id: UUID, now: datetime) -> None:
        """Итог успешной обработки: pending → done (лишний вызов — no-op).

        Один прямой UPDATE: строка не читается (payload до 1 МБ не гоняется
        по сети), атомарность даёт сам UPDATE — отдельная блокировка не нужна.
        """
        await self._session.execute(
            update(TelegramUpdateInbox)
            .where(
                TelegramUpdateInbox.id == inbox_id,
                TelegramUpdateInbox.status == TelegramInboxStatus.PENDING,
            )
            .values(status=TelegramInboxStatus.DONE, next_attempt_at=None)
        )

    async def record_failure(self, inbox_id: UUID, *, max_attempts: int) -> None:
        """Сбой обработки: попытка уже учтена claim'ом (lease), здесь только
        решение «потолок → failed», иначе строка ждёт свой бэкофф pending'ом.

        Ниже потолка строке менять нечего (next_attempt_at выставлен при
        claim'е), поэтому хватает одного условного UPDATE без чтения строки.
        """
        await self._session.execute(
            update(TelegramUpdateInbox)
            .where(
                TelegramUpdateInbox.id == inbox_id,
                TelegramUpdateInbox.status == TelegramInboxStatus.PENDING,
                TelegramUpdateInbox.attempt_count >= max_attempts,
            )
            .values(status=TelegramInboxStatus.FAILED)
        )

    async def read_status(self, now: datetime, *, bot_id: int) -> TelegramInboxHealth:
        """Глубина pending/failed и возраст головы — для статуса/монитора.

        Фильтр по боту и «не done»: агрегат ходит только по живым строкам
        (ложится на индекс bot_id/status), вечно растущая done-история
        не сканируется.
        """
        rows = (
            await self._session.execute(
                select(
                    TelegramUpdateInbox.status,
                    func.count(),
                    func.min(TelegramUpdateInbox.received_at),
                )
                .where(
                    TelegramUpdateInbox.bot_id == bot_id,
                    TelegramUpdateInbox.status != TelegramInboxStatus.DONE,
                )
                .group_by(TelegramUpdateInbox.status)
            )
        ).all()
        pending_count = 0
        failed_count = 0
        oldest_pending: datetime | None = None
        for status, count, oldest in rows:
            if status is TelegramInboxStatus.PENDING:
                pending_count = count
                oldest_pending = oldest
            elif status is TelegramInboxStatus.FAILED:
                failed_count = count
        head_age = (
            (now - oldest_pending).total_seconds()
            if oldest_pending is not None
            else None
        )
        return TelegramInboxHealth(
            pending_count=pending_count,
            failed_count=failed_count,
            head_age_seconds=head_age,
        )

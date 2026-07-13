from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.retrieval.application.contracts import (
    ConsumeSearchQueryCommand,
    SetAwaitingSearchCommand,
)
from second_brain.slices.retrieval.application.exact_search import ExactSearch
from second_brain.slices.retrieval.domain.entities import (
    MatchQuality,
    SearchRecord,
    SearchRecordType,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)


def record(index: int) -> SearchRecord:
    return SearchRecord(
        id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
        record_type=SearchRecordType.NOTE,
        text=f"private-result-{index}",
        source_capture_event_id=UUID(f"10000000-0000-0000-0000-{index:012d}"),
        created_at=NOW + timedelta(minutes=index),
        task_completed=None,
        match_quality=MatchQuality.FULL_TEXT,
    )


class InMemoryExactSearchStore:
    def __init__(self, records: tuple[SearchRecord, ...] = ()) -> None:
        self.pending = False
        self.records = records
        self.queries: list[tuple[AccessContext, str, int]] = []

    async def set_awaiting(self, _command: SetAwaitingSearchCommand) -> None:
        self.pending = True

    async def cancel(self, _access_context: AccessContext) -> None:
        self.pending = False

    async def lock_pending(self, _access_context: AccessContext) -> bool:
        return self.pending

    async def search(
        self, access_context: AccessContext, query: str, limit: int
    ) -> tuple[SearchRecord, ...]:
        self.queries.append((access_context, query, limit))
        return self.records[:limit]


@pytest.mark.asyncio
async def test_text_without_pending_search_is_not_consumed() -> None:
    store = InMemoryExactSearchStore()

    result = await ExactSearch(store).consume_query(
        ConsumeSearchQueryCommand(ACCESS, "PostgreSQL")
    )

    assert result is None
    assert store.queries == []


@pytest.mark.asyncio
async def test_valid_query_is_normalized_limited_and_consumes_pending_mode() -> None:
    store = InMemoryExactSearchStore(tuple(record(index) for index in range(1, 12)))
    search = ExactSearch(store)
    await search.set_awaiting(SetAwaitingSearchCommand(ACCESS, NOW, "1" * 32))

    result = await search.consume_query(
        ConsumeSearchQueryCommand(ACCESS, "  PostgreSQL   search  ")
    )

    assert result is not None
    assert result.query_required is False
    assert result.items == tuple(record(index) for index in range(1, 11))
    assert store.queries == [(ACCESS, "PostgreSQL search", 10)]
    assert store.pending is False


@pytest.mark.asyncio
async def test_whitespace_query_keeps_pending_mode_and_requests_text() -> None:
    store = InMemoryExactSearchStore()
    search = ExactSearch(store)
    await search.set_awaiting(SetAwaitingSearchCommand(ACCESS, NOW, "1" * 32))

    result = await search.consume_query(ConsumeSearchQueryCommand(ACCESS, " \n  "))

    assert result is not None
    assert result.query_required is True
    assert result.items == ()
    assert store.queries == []
    assert store.pending is True


def test_search_content_is_absent_from_repr() -> None:
    private_record = record(1)
    command = ConsumeSearchQueryCommand(ACCESS, "private-query")

    assert "private-query" not in repr(command)
    assert "private-result-1" not in repr(private_record)

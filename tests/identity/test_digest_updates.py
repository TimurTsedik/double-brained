"""Обработка callback'ов сводки (digest:*) в LocalUpdateProcessor.

Строгий парс: digest:menu, digest:(week|month|half_year|year) и
digest:more:(период):(offset):(as_of) — offset только беззнаковое десятичное без
ведущих нулей ограниченной длины, as_of только unix-секунды. Любой иной digest:*
гасится ДО любой другой обработки; offset за концом снимка и as_of из будущего —
IGNORED, неотличимо от мусора. Тексты записей не попадают в repr/логи.
"""

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from second_brain.slices.identity.adapters.telegram.dto import TelegramUpdate
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.identity.application.local_updates import (
    AcknowledgementKind,
    LocalUpdateProcessor,
)
from second_brain.slices.identity.ports.repositories import (
    NewUpdateResult,
    StoredUpdateReceipt,
)
from second_brain.slices.retrieval.application.contracts import (
    DigestCounters,
    DigestPage,
    DigestPeriod,
    RecordView,
    SearchRecordType,
)

TZ = ZoneInfo("Asia/Jerusalem")
# Микросекунды НЕ нулевые: as_of обязан усечься до целой секунды (в callback
# «Ещё» едут unix-секунды, фильтр всех страниц должен совпадать бит-в-бит).
NOW = datetime(2026, 7, 15, 12, 0, 0, 654321, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)


def make_page(
    offset: int = 0,
    total: int = 1,
    items: tuple[RecordView, ...] = (
        RecordView(
            id=UUID("00000000-0000-0000-0000-000000000301"),
            record_type=SearchRecordType.NOTE,
            text="secret digest text",
            created_at=NOW.astimezone(TZ),
            task_completed=None,
        ),
    ),
    period: DigestPeriod = DigestPeriod.WEEK,
) -> DigestPage:
    return DigestPage(
        period=period,
        period_start=datetime(2026, 7, 13, tzinfo=TZ),
        as_of=NOW.replace(microsecond=0).astimezone(TZ),
        offset=offset,
        total=total,
        counters=DigestCounters(
            notes=total, tasks=0, tasks_completed=0, ideas=0, decisions=0, questions=0
        ),
        items=items,
    )


class FixedClock:
    def now(self) -> datetime:
        return NOW


class KnownActorStore:
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        handler: object,
    ) -> StoredUpdateReceipt:
        result = await handler(self)
        assert isinstance(result, NewUpdateResult)
        return StoredUpdateReceipt(
            result.result_kind,
            result.trace_id,
            existing=False,
            span_id=result.span_id,
        )

    async def resolve_access_context(self, _telegram_user_id: int) -> AccessContext:
        return ACCESS

    async def read_user_space_language(
        self, _access_context: AccessContext
    ) -> str | None:
        return "ru"


class DuplicateDigestShownStore(KnownActorStore):
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        _handler: object,
    ) -> StoredUpdateReceipt:
        return StoredUpdateReceipt("digest_shown", "1" * 32, existing=True)


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(
        self, _telegram_user_id: int
    ) -> AccessContext | None:
        return None


class RecordingDigestPort:
    def __init__(self, page: DigestPage | None = None) -> None:
        self._page = page
        self.calls: list[tuple[AccessContext, DigestPeriod, int, datetime]] = []

    async def read_digest_page(
        self,
        access_context: AccessContext,
        period: DigestPeriod,
        offset: int,
        as_of: datetime,
        _transaction: UpdateTransaction,
    ) -> DigestPage:
        self.calls.append((access_context, period, offset, as_of))
        return self._page if self._page is not None else make_page(offset=offset)


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


def processor(
    store: KnownActorStore | None = None,
    digest_port: RecordingDigestPort | None = None,
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        store or KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        digest_port=digest_port,
    )


@pytest.mark.asyncio
async def test_digest_menu_shows_the_period_chooser_without_reading_data() -> None:
    port = RecordingDigestPort()

    result = await processor(digest_port=port).process(callback(800, "digest:menu"))

    assert result.kind is AcknowledgementKind.DIGEST_MENU_SHOWN
    assert result.fresh is True
    assert result.digest_page is None
    assert port.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("period", list(DigestPeriod))
async def test_period_selection_snapshots_now_to_whole_seconds(
    period: DigestPeriod,
) -> None:
    port = RecordingDigestPort()

    result = await processor(digest_port=port).process(
        callback(801, f"digest:{period.value}")
    )

    assert result.kind is AcknowledgementKind.DIGEST_SHOWN
    assert result.digest_page is not None
    assert port.calls == [(ACCESS, period, 0, NOW.replace(microsecond=0))]


@pytest.mark.asyncio
async def test_more_callback_parses_period_offset_and_as_of() -> None:
    port = RecordingDigestPort(page=make_page(offset=10, total=25))
    as_of_unix = int(NOW.timestamp()) - 60

    result = await processor(digest_port=port).process(
        callback(802, f"digest:more:month:10:{as_of_unix}")
    )

    assert result.kind is AcknowledgementKind.DIGEST_SHOWN
    assert result.digest_page is not None
    assert port.calls == [
        (ACCESS, DigestPeriod.MONTH, 10, datetime.fromtimestamp(as_of_unix, UTC))
    ]


@pytest.mark.asyncio
async def test_more_offset_past_the_snapshot_end_is_ignored() -> None:
    port = RecordingDigestPort(page=make_page(offset=30, total=25, items=()))
    as_of_unix = int(NOW.timestamp()) - 60

    result = await processor(digest_port=port).process(
        callback(803, f"digest:more:week:30:{as_of_unix}")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.digest_page is None


@pytest.mark.asyncio
async def test_more_with_a_future_as_of_is_ignored_without_a_read() -> None:
    port = RecordingDigestPort()
    future_unix = int(NOW.timestamp()) + 3600

    result = await processor(digest_port=port).process(
        callback(804, f"digest:more:week:10:{future_unix}")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.digest_page is None
    assert port.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        "digest:",
        "digest:day",
        "digest:WEEK",
        "digest:week:extra",
        "digest:menu:extra",
        "digest:more",
        "digest:more:",
        "digest:more:week",
        "digest:more:week:10",
        "digest:more:week:10:",
        "digest:more:banana:10:1784000000",
        "digest:more:week:-1:1784000000",
        "digest:more:week:01:1784000000",
        "digest:more:week:1000000:1784000000",
        "digest:more:week:10:0",
        "digest:more:week:10:01784000000",
        "digest:more:week:10:17840000000",
        "digest:more:week:10:1784000000:extra",
        "digest:more:week:10:1784000000 ",
        "digest:more:week:ten:1784000000",
    ],
)
async def test_malformed_digest_callback_is_rejected_before_any_processing(
    callback_data: str,
) -> None:
    # Порт вернул бы страницу — доказываем, что до него дело не доходит.
    port = RecordingDigestPort()

    result = await processor(digest_port=port).process(callback(805, callback_data))

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.digest_page is None
    assert port.calls == []


@pytest.mark.asyncio
async def test_digest_callbacks_without_a_wired_port_are_ignored() -> None:
    for update_id, data in (
        (806, "digest:menu"),
        (807, "digest:week"),
        (808, f"digest:more:week:10:{int(NOW.timestamp()) - 60}"),
    ):
        result = await processor().process(callback(update_id, data))
        assert result.kind is AcknowledgementKind.IGNORED
        assert result.digest_page is None


@pytest.mark.asyncio
async def test_digest_callback_from_unknown_actor_is_ignored() -> None:
    port = RecordingDigestPort()

    result = await processor(store=UnknownActorStore(), digest_port=port).process(
        callback(809, "digest:week")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert port.calls == []


@pytest.mark.asyncio
async def test_replay_of_digest_shown_stays_silent() -> None:
    port = RecordingDigestPort()

    result = await processor(
        store=DuplicateDigestShownStore(), digest_port=port
    ).process(callback(810, "digest:week"))

    assert result.kind is AcknowledgementKind.DIGEST_SHOWN
    assert result.fresh is False
    assert result.digest_page is None
    assert port.calls == []


@pytest.mark.asyncio
async def test_garbage_foreign_and_past_end_are_indistinguishable() -> None:
    as_of_unix = int(NOW.timestamp()) - 60
    garbage = await processor(digest_port=RecordingDigestPort()).process(
        callback(811, "digest:more:week:zz:1")
    )
    past_end = await processor(
        digest_port=RecordingDigestPort(page=make_page(offset=99, total=1, items=()))
    ).process(callback(812, f"digest:more:week:99:{as_of_unix}"))
    unwired = await processor().process(callback(813, "digest:week"))

    observable = {
        (result.kind, result.fresh, result.digest_page)
        for result in (garbage, past_end, unwired)
    }
    assert observable == {(AcknowledgementKind.IGNORED, True, None)}


@pytest.mark.asyncio
async def test_digest_payload_never_leaks_record_text_in_repr() -> None:
    port = RecordingDigestPort()

    result = await processor(digest_port=port).process(callback(814, "digest:week"))

    assert result.digest_page is not None
    for rendered in (repr(result), repr(result.digest_page)):
        assert "secret" not in rendered

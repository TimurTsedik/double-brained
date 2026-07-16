"""Обработка callback'а «показать целиком» (show:тип:uuid) в LocalUpdateProcessor.

Строгий парс: принимается ТОЛЬКО show:(note|task|idea|decision|question):<uuid>
(нижний регистр); любой иной show:* гасится ДО любой другой обработки. Чужой /
несуществующий / мусорный callback неразличимы: IGNORED, ни одного сообщения,
никакого payload'а. Полный текст записи не попадает в repr/логи.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

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
    RecordView,
    RecordViewResult,
    SearchRecordType,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
RECORD = RecordView(
    id=UUID("00000000-0000-0000-0000-000000000301"),
    record_type=SearchRecordType.NOTE,
    text="secret full text",
    created_at=NOW,
    task_completed=None,
)
RELATED = (
    RecordView(
        id=UUID("00000000-0000-0000-0000-000000000302"),
        record_type=SearchRecordType.TASK,
        text="secret related task",
        created_at=NOW,
        task_completed=True,
    ),
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


class DuplicateRecordShownStore(KnownActorStore):
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        _handler: object,
    ) -> StoredUpdateReceipt:
        return StoredUpdateReceipt("record_shown", "1" * 32, existing=True)


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(
        self, _telegram_user_id: int
    ) -> AccessContext | None:
        return None


class RecordingRecordViewPort:
    def __init__(
        self,
        record: RecordView | None = None,
        related: tuple[RecordView, ...] = (),
    ) -> None:
        self._record = record
        self._related = related
        self.read_calls: list[tuple[AccessContext, SearchRecordType, UUID]] = []
        self.related_calls: list[tuple[AccessContext, SearchRecordType, UUID]] = []

    async def read_record_full(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        _transaction: UpdateTransaction,
    ) -> RecordView | None:
        self.read_calls.append((access_context, record_type, record_id))
        return self._record

    async def related_records(
        self,
        access_context: AccessContext,
        record_type: SearchRecordType,
        record_id: UUID,
        _transaction: UpdateTransaction,
    ) -> tuple[RecordView, ...]:
        self.related_calls.append((access_context, record_type, record_id))
        return self._related


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
    record_view_port: RecordingRecordViewPort | None = None,
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        store or KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        record_view_port=record_view_port,
    )


@pytest.mark.asyncio
async def test_show_callback_reads_record_and_related() -> None:
    port = RecordingRecordViewPort(record=RECORD, related=RELATED)

    result = await processor(record_view_port=port).process(
        callback(500, f"show:note:{RECORD.id}")
    )

    assert result.kind is AcknowledgementKind.RECORD_SHOWN
    assert result.fresh is True
    assert result.record_view == RecordViewResult(record=RECORD, related=RELATED)
    assert port.read_calls == [(ACCESS, SearchRecordType.NOTE, RECORD.id)]
    assert port.related_calls == [(ACCESS, SearchRecordType.NOTE, RECORD.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", list(SearchRecordType))
async def test_show_callback_parses_every_record_type(
    record_type: SearchRecordType,
) -> None:
    port = RecordingRecordViewPort(record=RECORD)
    record_id = uuid4()

    result = await processor(record_view_port=port).process(
        callback(501, f"show:{record_type.value}:{record_id}")
    )

    assert result.kind is AcknowledgementKind.RECORD_SHOWN
    assert port.read_calls == [(ACCESS, record_type, record_id)]


@pytest.mark.asyncio
async def test_unknown_record_is_ignored_without_a_related_lookup() -> None:
    port = RecordingRecordViewPort(record=None)

    result = await processor(record_view_port=port).process(
        callback(502, f"show:note:{uuid4()}")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.record_view is None
    assert len(port.read_calls) == 1
    assert port.related_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        "show:",
        "show:note",
        "show:note:",
        "show:banana:00000000-0000-0000-0000-000000000301",
        "show:note:not-a-uuid",
        "show:note:00000000-0000-0000-0000-000000000301:extra",
        "show:note:00000000-0000-0000-0000-000000000301 ",
        "show:note:00000000-0000-0000-0000-0000003012AB",
        "show:NOTE:00000000-0000-0000-0000-000000000301",
        "show:note:00000000-0000-0000-0000-0000003012AB".upper(),
    ],
)
async def test_malformed_show_callback_is_rejected_before_any_processing(
    callback_data: str,
) -> None:
    # Порт вернул бы запись — доказываем, что до него дело не доходит.
    port = RecordingRecordViewPort(record=RECORD, related=RELATED)

    result = await processor(record_view_port=port).process(
        callback(503, callback_data)
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.record_view is None
    assert port.read_calls == []
    assert port.related_calls == []


@pytest.mark.asyncio
async def test_foreign_unknown_and_garbage_show_are_indistinguishable() -> None:
    # Чужой uuid неотличим от несуществующего под RLS: порт возвращает None в
    # обоих случаях. Мусор гасится ещё раньше. Наружу — один и тот же результат.
    foreign = await processor(
        record_view_port=RecordingRecordViewPort(record=None)
    ).process(callback(504, f"show:note:{uuid4()}"))
    unknown = await processor(
        record_view_port=RecordingRecordViewPort(record=None)
    ).process(callback(505, f"show:task:{uuid4()}"))
    garbage = await processor(
        record_view_port=RecordingRecordViewPort(record=RECORD)
    ).process(callback(506, "show:note:garbage"))
    unwired = await processor().process(callback(507, f"show:note:{uuid4()}"))

    observable = {
        (
            result.kind,
            result.fresh,
            result.record_view,
            result.task_panel,
            result.search_panel,
            result.project_panel,
        )
        for result in (foreign, unknown, garbage, unwired)
    }
    assert observable == {(AcknowledgementKind.IGNORED, True, None, None, None, None)}


@pytest.mark.asyncio
async def test_replay_of_record_shown_stays_silent() -> None:
    port = RecordingRecordViewPort(record=RECORD, related=RELATED)

    result = await processor(
        store=DuplicateRecordShownStore(), record_view_port=port
    ).process(callback(508, f"show:note:{RECORD.id}"))

    assert result.kind is AcknowledgementKind.RECORD_SHOWN
    assert result.fresh is False
    assert result.record_view is None
    assert port.read_calls == []
    assert port.related_calls == []


@pytest.mark.asyncio
async def test_show_callback_from_unknown_actor_is_ignored() -> None:
    port = RecordingRecordViewPort(record=RECORD)

    result = await processor(store=UnknownActorStore(), record_view_port=port).process(
        callback(509, f"show:note:{RECORD.id}")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert port.read_calls == []


@pytest.mark.asyncio
async def test_record_view_payload_never_leaks_text_in_repr() -> None:
    port = RecordingRecordViewPort(record=RECORD, related=RELATED)

    result = await processor(record_view_port=port).process(
        callback(510, f"show:note:{RECORD.id}")
    )

    assert result.record_view is not None
    for rendered in (repr(result), repr(result.record_view), repr(RECORD)):
        assert "secret" not in rendered

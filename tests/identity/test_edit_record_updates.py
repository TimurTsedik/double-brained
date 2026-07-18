"""Маршрутизация правки записи (S3): callback «✏️ Править» и consume текста.

Строгий парс: принимается ТОЛЬКО edit:(note|task|idea|decision|question):<uuid>
(нижний регистр) и edit:cancel; любой иной edit:* гасится ДО любой другой
обработки. Чужой / несуществующий / мусорный callback неразличимы: IGNORED,
режим НЕ ставится. Consume: следующий текст становится новым текстом записи —
БЕЗ капчи, без классификации; payload несёт только reminder_when.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from second_brain.slices.capture.application.contracts import TelegramLink
from second_brain.slices.editing.application.contracts import (
    BeginRecordEditCommand,
    ConsumeRecordEditCommand,
    RecordEditResult,
)
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
from second_brain.slices.retrieval.application.contracts import SearchRecordType

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000011"),
)
RECORD_ID = UUID("00000000-0000-0000-0000-000000000301")
REMINDER_WHEN = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)


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
        result = await handler(self)  # type: ignore[operator]
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


class DuplicateRecordEditedStore(KnownActorStore):
    async def process_once(
        self,
        _bot_id: int,
        _update_id: int,
        _occurred_at: datetime,
        _handler: object,
    ) -> StoredUpdateReceipt:
        return StoredUpdateReceipt("record_edited", "1" * 32, existing=True)


class UnknownActorStore(KnownActorStore):
    async def resolve_access_context(
        self, _telegram_user_id: int
    ) -> AccessContext | None:
        return None


class RecordingEditPort:
    """Фейковый RecordEditPort: пишет вызовы, отдаёт настроенные исходы."""

    def __init__(
        self,
        *,
        begin_result: bool = True,
        consume_result: RecordEditResult | None = None,
    ) -> None:
        self._begin_result = begin_result
        self._consume_result = consume_result
        self.begin_calls: list[BeginRecordEditCommand] = []
        self.cancel_calls: list[AccessContext] = []
        self.consume_calls: list[ConsumeRecordEditCommand] = []

    async def begin(
        self, command: BeginRecordEditCommand, _transaction: UpdateTransaction
    ) -> bool:
        self.begin_calls.append(command)
        return self._begin_result

    async def cancel(
        self, access_context: AccessContext, _transaction: UpdateTransaction
    ) -> None:
        self.cancel_calls.append(access_context)

    async def consume_new_text(
        self, command: ConsumeRecordEditCommand, _transaction: UpdateTransaction
    ) -> RecordEditResult | None:
        self.consume_calls.append(command)
        return self._consume_result


class RecordingCapturePort:
    def __init__(self) -> None:
        self.captured: list[str] = []

    async def capture(self, command: object, _transaction: object) -> object:
        self.captured.append(getattr(command, "raw_text", ""))

        class _Source:
            id = uuid4()

        return _Source()


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


def text_update(update_id: int, value: str) -> TelegramUpdate:
    return TelegramUpdate(
        1,
        update_id,
        True,
        42,
        value,
        telegram_message_id=update_id + 1_000,
        links=(TelegramLink(label="тут", url="https://a.example/x"),),
    )


def processor(
    store: KnownActorStore | None = None,
    edit_port: RecordingEditPort | None = None,
    capture_port: RecordingCapturePort | None = None,
) -> LocalUpdateProcessor:
    return LocalUpdateProcessor(
        store or KnownActorStore(),
        FixedClock(),
        b"pepper",
        "key",
        capture_text_port=capture_port,  # type: ignore[arg-type]
        record_edit_port=edit_port,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", list(SearchRecordType))
async def test_edit_callback_sets_the_mode_for_every_record_type(
    record_type: SearchRecordType,
) -> None:
    port = RecordingEditPort()
    record_id = uuid4()

    result = await processor(edit_port=port).process(
        callback(600, f"edit:{record_type.value}:{record_id}")
    )

    assert result.kind is AcknowledgementKind.EDIT_MODE_SET
    assert result.fresh is True
    assert [(c.record_kind, c.record_id) for c in port.begin_calls] == [
        (record_type, record_id)
    ]
    assert port.begin_calls[0].access_context == ACCESS


@pytest.mark.asyncio
async def test_foreign_or_unknown_record_leaves_no_mode_and_is_ignored() -> None:
    # Порт вернул False (чужая/несуществующая запись под RLS): IGNORED,
    # наружу неотличимо от мусора.
    port = RecordingEditPort(begin_result=False)

    result = await processor(edit_port=port).process(
        callback(601, f"edit:note:{uuid4()}")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert len(port.begin_calls) == 1
    assert port.consume_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        "edit:",
        "edit:note",
        "edit:note:",
        "edit:banana:00000000-0000-0000-0000-000000000301",
        "edit:note:not-a-uuid",
        "edit:note:00000000-0000-0000-0000-000000000301:extra",
        "edit:note:00000000-0000-0000-0000-000000000301 ",
        "edit:NOTE:00000000-0000-0000-0000-000000000301",
        "edit:cancel:extra",
    ],
)
async def test_malformed_edit_callback_is_rejected_before_any_processing(
    callback_data: str,
) -> None:
    port = RecordingEditPort()

    result = await processor(edit_port=port).process(callback(602, callback_data))

    assert result.kind is AcknowledgementKind.IGNORED
    assert port.begin_calls == []
    assert port.cancel_calls == []


@pytest.mark.asyncio
async def test_edit_cancel_clears_the_mode() -> None:
    port = RecordingEditPort()

    result = await processor(edit_port=port).process(callback(603, "edit:cancel"))

    assert result.kind is AcknowledgementKind.EDIT_MODE_CANCELLED
    assert port.cancel_calls == [ACCESS]
    assert port.begin_calls == []


@pytest.mark.asyncio
async def test_edit_callback_from_unknown_actor_is_ignored() -> None:
    port = RecordingEditPort()

    result = await processor(store=UnknownActorStore(), edit_port=port).process(
        callback(604, f"edit:note:{RECORD_ID}")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert port.begin_calls == []


@pytest.mark.asyncio
async def test_pending_edit_consumes_the_next_text_instead_of_capturing() -> None:
    # Следующий текст = новый текст записи: капча НЕ создаётся, payload несёт
    # reminder_when для строки «⏰ напоминание осталось…».
    port = RecordingEditPort(
        consume_result=RecordEditResult(
            record_kind=SearchRecordType.TASK,
            record_id=RECORD_ID,
            reminder_when=REMINDER_WHEN,
        )
    )
    capture = RecordingCapturePort()

    result = await processor(edit_port=port, capture_port=capture).process(
        text_update(605, "новый текст задачи")
    )

    assert result.kind is AcknowledgementKind.RECORD_EDITED
    assert result.reminder_when == REMINDER_WHEN
    assert capture.captured == []
    assert [(c.text, c.links) for c in port.consume_calls] == [
        (
            "новый текст задачи",
            (TelegramLink(label="тут", url="https://a.example/x"),),
        )
    ]


@pytest.mark.asyncio
async def test_without_pending_edit_the_text_captures_as_usual() -> None:
    port = RecordingEditPort(consume_result=None)
    capture = RecordingCapturePort()

    result = await processor(edit_port=port, capture_port=capture).process(
        text_update(606, "обычная заметка")
    )

    assert result.kind is AcknowledgementKind.CAPTURED
    assert capture.captured == ["обычная заметка"]


@pytest.mark.asyncio
async def test_vanished_record_on_consume_is_ignored_without_capture() -> None:
    port = RecordingEditPort(
        consume_result=RecordEditResult(
            record_kind=SearchRecordType.NOTE,
            record_id=RECORD_ID,
            record_missing=True,
        )
    )
    capture = RecordingCapturePort()

    result = await processor(edit_port=port, capture_port=capture).process(
        text_update(607, "текст в никуда")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert result.reminder_when is None
    assert capture.captured == []


@pytest.mark.asyncio
async def test_slash_command_clears_the_pending_edit_mode() -> None:
    # «/команда» — не новый текст записи: режим гасится, иначе следующее
    # сообщение неожиданно перезаписало бы запись.
    port = RecordingEditPort()
    capture = RecordingCapturePort()

    result = await processor(edit_port=port, capture_port=capture).process(
        text_update(620, "/help")
    )

    assert result.kind is AcknowledgementKind.IGNORED
    assert port.cancel_calls == [ACCESS]
    assert port.consume_calls == []
    assert capture.captured == []


@pytest.mark.asyncio
async def test_start_command_clears_the_pending_edit_mode() -> None:
    port = RecordingEditPort()

    result = await processor(edit_port=port).process(text_update(621, "/start"))

    assert result.kind is AcknowledgementKind.PANEL_SHOWN
    assert port.cancel_calls == [ACCESS]


@pytest.mark.asyncio
async def test_whitespace_text_keeps_the_mode_and_reprompts() -> None:
    # Пробельный «новый текст» не потребляет режим: порт отвечает
    # text_required, наружу — тот же промпт режима (EDIT_MODE_SET).
    port = RecordingEditPort(
        consume_result=RecordEditResult(
            record_kind=SearchRecordType.NOTE,
            record_id=RECORD_ID,
            text_required=True,
        )
    )
    capture = RecordingCapturePort()

    result = await processor(edit_port=port, capture_port=capture).process(
        text_update(622, "   ")
    )

    assert result.kind is AcknowledgementKind.EDIT_MODE_SET
    assert result.reminder_when is None
    assert capture.captured == []


@pytest.mark.asyncio
async def test_replay_of_record_edited_stays_silent_without_payload() -> None:
    port = RecordingEditPort(
        consume_result=RecordEditResult(
            record_kind=SearchRecordType.TASK,
            record_id=RECORD_ID,
            reminder_when=REMINDER_WHEN + timedelta(hours=1),
        )
    )

    result = await processor(
        store=DuplicateRecordEditedStore(), edit_port=port
    ).process(text_update(608, "повтор апдейта"))

    assert result.kind is AcknowledgementKind.RECORD_EDITED
    assert result.fresh is False
    assert result.reminder_when is None
    assert port.consume_calls == []

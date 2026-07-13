from datetime import UTC, datetime
from re import fullmatch

import pytest

from second_brain.shared.clock import Clock, SystemClock
from second_brain.shared.trace import TraceContext


class FixedClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def read_now(clock: Clock) -> datetime:
    return clock.now()


def test_clock_contract_supports_an_injected_implementation() -> None:
    instant = datetime(2026, 7, 12, tzinfo=UTC)

    assert read_now(FixedClock(instant)) == instant


def test_system_clock_returns_timezone_aware_utc_now() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_root_trace_has_w3c_valid_nonzero_identifiers() -> None:
    root = TraceContext.new_root()

    assert fullmatch(r"[0-9a-f]{32}", root.trace_id)
    assert root.trace_id != "0" * 32
    assert fullmatch(r"[0-9a-f]{16}", root.span_id)
    assert root.span_id != "0" * 16


@pytest.mark.parametrize(
    "trace_id",
    [
        "1" * 31,
        "A" + "1" * 31,
        "g" * 32,
        "0" * 32,
    ],
)
def test_trace_context_rejects_an_invalid_trace_id(trace_id: str) -> None:
    with pytest.raises(ValueError, match="trace_id"):
        TraceContext(trace_id=trace_id, span_id="1" * 16)


@pytest.mark.parametrize(
    "span_id",
    [
        "1" * 15,
        "A" + "1" * 15,
        "g" * 16,
        "0" * 16,
    ],
)
def test_trace_context_rejects_an_invalid_span_id(span_id: str) -> None:
    with pytest.raises(ValueError, match="span_id"):
        TraceContext(trace_id="1" * 32, span_id=span_id)


def test_retry_trace_keeps_trace_id_and_gets_new_span() -> None:
    root = TraceContext.new_root()
    retry = root.new_attempt()

    assert retry.trace_id == root.trace_id
    assert retry.span_id != root.span_id


def test_retry_trace_regenerates_a_colliding_span_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = TraceContext(trace_id="1" * 32, span_id="a" * 16)
    span_ids = iter([root.span_id, "b" * 16])
    monkeypatch.setattr(
        "second_brain.shared.trace.token_hex",
        lambda _byte_count: next(span_ids),
    )

    retry = root.new_attempt()

    assert retry.span_id == "b" * 16

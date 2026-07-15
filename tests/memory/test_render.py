from datetime import UTC, datetime
from uuid import uuid4

from second_brain.slices.memory.application.render import (
    render_answer,
    render_safe_failure,
)
from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    MemoryAnswer,
    MemoryRecordKind,
)

_CREATED = datetime(2026, 7, 15, tzinfo=UTC)


def source(kind: MemoryRecordKind = MemoryRecordKind.NOTE) -> AnswerSource:
    return AnswerSource(
        label="S1",
        record_kind=kind,
        record_id=uuid4(),
        source_capture_event_id=uuid4(),
        created_at=_CREATED,
    )


def answer(
    level: EvidenceLevel,
    text: str = "Сводный вывод",
    sources: tuple[AnswerSource, ...] = (),
) -> MemoryAnswer:
    return MemoryAnswer(
        evidence_level=level,
        answer_text=text,
        sources=sources,
        model_name="nvidia/nemotron",
        prompt_version="grounded-answer-v1",
        schema_version="grounded-answer-v1",
    )


def test_direct_answer_leads_with_conclusion_and_trusted_badge() -> None:
    rendered = render_answer(
        answer(
            EvidenceLevel.DIRECT, "Пассажир — Иван", (source(MemoryRecordKind.NOTE),)
        )
    )

    assert rendered.startswith("Пассажир — Иван")
    assert "✅ Прямо из заметок" in rendered
    assert "Заметка · 15.07.2026" in rendered


def test_reconstructed_uses_its_badge() -> None:
    rendered = render_answer(
        answer(EvidenceLevel.RECONSTRUCTED, sources=(source(MemoryRecordKind.TASK),))
    )

    assert "🧩 Собрано по кусочкам" in rendered
    assert "Задача · 15.07.2026" in rendered


def test_hypothesis_uses_its_badge() -> None:
    rendered = render_answer(
        answer(EvidenceLevel.HYPOTHESIS, sources=(source(MemoryRecordKind.IDEA),))
    )

    assert "💭 Предположение" in rendered
    assert "Идея · 15.07.2026" in rendered


def test_insufficient_shows_empty_memory_badge_without_sources() -> None:
    rendered = render_answer(answer(EvidenceLevel.INSUFFICIENT, ""))

    assert "∅ В памяти нет ответа" in rendered
    assert "Источники" not in rendered


def test_render_answer_leaks_no_raw_identifiers() -> None:
    one = source(MemoryRecordKind.DECISION)
    rendered = render_answer(answer(EvidenceLevel.DIRECT, sources=(one,)))

    assert str(one.record_id) not in rendered
    assert str(one.source_capture_event_id) not in rendered


def test_render_safe_failure_has_trace_reference_and_no_content() -> None:
    rendered = render_safe_failure("trace-abc-123")

    assert "trace-abc-123" in rendered
    assert "Сводный вывод" not in rendered
    assert "✅" not in rendered

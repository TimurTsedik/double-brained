import string
from datetime import UTC, datetime
from uuid import uuid4

from second_brain.shared.i18n import Locale
from second_brain.slices.memory.application.render import (
    _KIND_LABELS,
    _LEVEL_BADGES,
    _SAFE_FAILURE,
    _SOURCES_HEADER,
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


# --- RU regression: byte-for-byte identical to the pre-localization output. ---


def test_direct_answer_leads_with_conclusion_and_trusted_badge_ru() -> None:
    rendered = render_answer(
        answer(
            EvidenceLevel.DIRECT, "Пассажир — Иван", (source(MemoryRecordKind.NOTE),)
        ),
        Locale.RU,
    )

    assert rendered.startswith("Пассажир — Иван")
    assert "✅ Прямо из заметок" in rendered
    assert "Источники:" in rendered
    assert "Заметка · 15.07.2026" in rendered


def test_reconstructed_uses_its_badge_ru() -> None:
    rendered = render_answer(
        answer(EvidenceLevel.RECONSTRUCTED, sources=(source(MemoryRecordKind.TASK),)),
        Locale.RU,
    )

    assert "🧩 Собрано по кусочкам" in rendered
    assert "Задача · 15.07.2026" in rendered


def test_hypothesis_uses_its_badge_ru() -> None:
    rendered = render_answer(
        answer(EvidenceLevel.HYPOTHESIS, sources=(source(MemoryRecordKind.IDEA),)),
        Locale.RU,
    )

    assert "💭 Предположение" in rendered
    assert "Идея · 15.07.2026" in rendered


def test_insufficient_shows_empty_memory_badge_without_sources_ru() -> None:
    rendered = render_answer(answer(EvidenceLevel.INSUFFICIENT, ""), Locale.RU)

    assert rendered == "∅ В памяти нет ответа"
    assert "Источники" not in rendered


# --- EN: same data, English chrome. ---


def test_direct_answer_english_chrome() -> None:
    rendered = render_answer(
        answer(
            EvidenceLevel.DIRECT, "Passenger is Ivan", (source(MemoryRecordKind.NOTE),)
        ),
        Locale.EN,
    )

    assert rendered.startswith("Passenger is Ivan")
    assert _LEVEL_BADGES[Locale.EN][EvidenceLevel.DIRECT] in rendered
    assert "Sources:" in rendered
    assert "Источники" not in rendered
    # Deterministic English date, not system-locale dependent.
    note_label = _KIND_LABELS[Locale.EN][MemoryRecordKind.NOTE]
    assert f"{note_label} · Jul 15, 2026" in rendered


def test_insufficient_english_is_only_the_badge() -> None:
    rendered = render_answer(answer(EvidenceLevel.INSUFFICIENT, ""), Locale.EN)

    assert rendered == _LEVEL_BADGES[Locale.EN][EvidenceLevel.INSUFFICIENT]
    assert "Sources" not in rendered


def test_render_answer_leaks_no_raw_identifiers_both_locales() -> None:
    one = source(MemoryRecordKind.DECISION)
    for locale in Locale:
        rendered = render_answer(answer(EvidenceLevel.DIRECT, sources=(one,)), locale)
        assert str(one.record_id) not in rendered
        assert str(one.source_capture_event_id) not in rendered


def test_render_safe_failure_localized_with_trace_and_no_content() -> None:
    ru = render_safe_failure("trace-abc-123", Locale.RU)
    assert ru == "Не удалось подготовить ответ.\nКод обращения: trace-abc-123"

    en = render_safe_failure("trace-abc-123", Locale.EN)
    assert "trace-abc-123" in en
    assert en != ru
    assert "Не удалось" not in en
    for rendered in (ru, en):
        assert "Сводный вывод" not in rendered
        assert "✅" not in rendered


# --- Catalog completeness (owner decision 10). ---


def test_every_evidence_level_has_badge_on_both_locales() -> None:
    for locale in Locale:
        for level in EvidenceLevel:
            assert _LEVEL_BADGES[locale][level]


def test_every_record_kind_has_label_on_both_locales() -> None:
    for locale in Locale:
        for kind in MemoryRecordKind:
            assert _KIND_LABELS[locale][kind]


def test_sources_header_present_on_both_locales() -> None:
    for locale in Locale:
        assert _SOURCES_HEADER[locale]


def test_safe_failure_placeholder_parity() -> None:
    def placeholders(template: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(template) if name}

    ru = placeholders(_SAFE_FAILURE[Locale.RU])
    en = placeholders(_SAFE_FAILURE[Locale.EN])
    assert ru == en == {"trace_id"}

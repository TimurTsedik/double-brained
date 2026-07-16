from datetime import datetime

from second_brain.shared.i18n import Locale
from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    MemoryAnswer,
    MemoryRecordKind,
)

# Owner decision (2026-07-15): trust level shown with an icon AND plain words.
# Chrome is localized per user; the model answer text itself is NOT translated.
# RU values are byte-for-byte the pre-localization strings (regression).
_LEVEL_BADGES: dict[Locale, dict[EvidenceLevel, str]] = {
    Locale.RU: {
        EvidenceLevel.DIRECT: "✅ Прямо из заметок",
        EvidenceLevel.RECONSTRUCTED: "🧩 Собрано по кусочкам",
        EvidenceLevel.HYPOTHESIS: "💭 Предположение",
        EvidenceLevel.INSUFFICIENT: "∅ В памяти нет ответа",
    },
    Locale.EN: {
        EvidenceLevel.DIRECT: "✅ Straight from your notes",
        EvidenceLevel.RECONSTRUCTED: "🧩 Pieced together",
        EvidenceLevel.HYPOTHESIS: "💭 Best guess",
        EvidenceLevel.INSUFFICIENT: "∅ Nothing in memory",
    },
}

_KIND_LABELS: dict[Locale, dict[MemoryRecordKind, str]] = {
    Locale.RU: {
        MemoryRecordKind.NOTE: "Заметка",
        MemoryRecordKind.TASK: "Задача",
        MemoryRecordKind.IDEA: "Идея",
        MemoryRecordKind.DECISION: "Решение",
        MemoryRecordKind.QUESTION: "Вопрос",
    },
    Locale.EN: {
        MemoryRecordKind.NOTE: "Note",
        MemoryRecordKind.TASK: "Task",
        MemoryRecordKind.IDEA: "Idea",
        MemoryRecordKind.DECISION: "Decision",
        MemoryRecordKind.QUESTION: "Question",
    },
}

_SOURCES_HEADER: dict[Locale, str] = {
    Locale.RU: "Источники:",
    Locale.EN: "Sources:",
}

_SAFE_FAILURE: dict[Locale, str] = {
    Locale.RU: "Не удалось подготовить ответ.\nКод обращения: {trace_id}",
    Locale.EN: "Could not prepare an answer.\nReference code: {trace_id}",
}

# Hardcoded English month abbreviations: the EN date must be deterministic and
# independent of the process/system locale (strftime("%b") is locale-sensitive).
_EN_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _format_date(value: datetime, locale: Locale) -> str:
    if locale is Locale.EN:
        return f"{_EN_MONTHS[value.month - 1]} {value.day}, {value.year}"
    return value.strftime("%d.%m.%Y")


def render_source_label(source: AnswerSource, locale: Locale) -> str:
    # One "Источники:" line — also reused verbatim as the show-button label in
    # the delivery payload, so buttons and text always describe sources alike.
    kind = _KIND_LABELS[locale][source.record_kind]
    return f"{kind} · {_format_date(source.created_at, locale)}"


def render_answer(answer: MemoryAnswer, locale: Locale) -> str:
    badge = _LEVEL_BADGES[locale][answer.evidence_level]
    if answer.evidence_level is EvidenceLevel.INSUFFICIENT:
        return badge

    lines = [answer.answer_text.strip(), "", badge]
    if answer.sources:
        lines.append("")
        lines.append(_SOURCES_HEADER[locale])
        for source in answer.sources:
            lines.append(render_source_label(source, locale))
    return "\n".join(lines)


def render_safe_failure(trace_id: str, locale: Locale) -> str:
    return _SAFE_FAILURE[locale].format(trace_id=trace_id)

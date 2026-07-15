from second_brain.slices.memory.domain.entities import (
    EvidenceLevel,
    MemoryAnswer,
    MemoryRecordKind,
)

# Owner decision (2026-07-15): trust level shown with an icon AND plain words.
_LEVEL_BADGES = {
    EvidenceLevel.DIRECT: "✅ Прямо из заметок",
    EvidenceLevel.RECONSTRUCTED: "🧩 Собрано по кусочкам",
    EvidenceLevel.HYPOTHESIS: "💭 Предположение",
    EvidenceLevel.INSUFFICIENT: "∅ В памяти нет ответа",
}

_KIND_LABELS = {
    MemoryRecordKind.NOTE: "Заметка",
    MemoryRecordKind.TASK: "Задача",
    MemoryRecordKind.IDEA: "Идея",
    MemoryRecordKind.DECISION: "Решение",
    MemoryRecordKind.QUESTION: "Вопрос",
}


def render_answer(answer: MemoryAnswer) -> str:
    badge = _LEVEL_BADGES[answer.evidence_level]
    if answer.evidence_level is EvidenceLevel.INSUFFICIENT:
        return badge

    lines = [answer.answer_text.strip(), "", badge]
    if answer.sources:
        lines.append("")
        lines.append("Источники:")
        for source in answer.sources:
            kind = _KIND_LABELS[source.record_kind]
            lines.append(f"{kind} · {source.created_at.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def render_safe_failure(trace_id: str) -> str:
    return f"Не удалось подготовить ответ.\nКод обращения: {trace_id}"

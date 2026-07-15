"""Локализованный каталог голосовых уведомлений worker-пути.

Каталог живёт РЯДОМ со своими доменными enum'ами (``TranscriptionOutputType`` /
``ProcessingNoticeKind``), а не в ``shared``: границы импортов запрещают
``shared`` тянуть доменные enum'ы слайсов. ``Locale`` — доменно-нейтральный тип
из ``shared`` — импортируется сюда свободно.

RU-строки дословно равны прежним литералам ``voice.py`` (регрессия зелёная);
EN — корректный перевод.
"""

from second_brain.shared.i18n import Locale
from second_brain.slices.processing.domain.entities import (
    ProcessingNoticeClaim,
    ProcessingNoticeKind,
    TranscriptionOutputType,
)

SUCCESS_LABELS: dict[TranscriptionOutputType, dict[Locale, str]] = {
    TranscriptionOutputType.NOTE: {Locale.RU: "📝 Заметка", Locale.EN: "📝 Note"},
    TranscriptionOutputType.TASK: {Locale.RU: "✅ Задача", Locale.EN: "✅ Task"},
    TranscriptionOutputType.IDEA: {Locale.RU: "💡 Идея", Locale.EN: "💡 Idea"},
    TranscriptionOutputType.DECISION: {
        Locale.RU: "⚖️ Решение",
        Locale.EN: "⚖️ Decision",
    },
    TranscriptionOutputType.QUESTION: {
        Locale.RU: "❓ Вопрос",
        Locale.EN: "❓ Question",
    },
}

CATALOG: dict[str, dict[Locale, str]] = {
    "notice.success": {
        Locale.RU: "🎙️ Расшифровано и сохранено: {label}.",
        Locale.EN: "🎙️ Transcribed and saved: {label}.",
    },
    "notice.failure": {
        Locale.RU: "Не удалось обработать запись.\nTrace ID: {trace_id}",
        Locale.EN: "Could not process the recording.\nTrace ID: {trace_id}",
    },
}


def notice_text(notice: ProcessingNoticeClaim, locale: Locale) -> str:
    """Собрать текст уведомления о голосе на указанном языке."""
    if notice.kind is ProcessingNoticeKind.SUCCESS:
        label = SUCCESS_LABELS[notice.output_type][locale]
        return CATALOG["notice.success"][locale].format(label=label)
    return CATALOG["notice.failure"][locale].format(trace_id=notice.trace_id)

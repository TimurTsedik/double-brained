"""Каталог всех пользовательских строк телеграм-транспорта на RU и EN.

Живёт рядом с доменными enum'ами identity (`AcknowledgementKind`,
`SearchRecordType`): `shared` не может импортировать доменные enum'ы слайсов,
поэтому каталог по этим enum'ам обязан жить в слайсе-владельце.

RU-значения совпадают ДОСЛОВНО с прежними строками транспорта — это держит
регрессию зелёной. EN — корректный английский. Параметризация через
`str.format(**params)`; множества плейсхолдеров `{...}` в RU и EN совпадают.
"""

from second_brain.shared.i18n import Locale
from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.retrieval.application.contracts import (
    SearchRecord,
    SearchRecordType,
)

# Единый словарь строк: ключ → {Locale.RU: ..., Locale.EN: ...}. Тесты полноты
# итерируют именно по нему (каждый ключ — оба языка, паритет плейсхолдеров).
CATALOG: dict[str, dict[Locale, str]] = {
    # --- панель ---
    "panel.prompt": {Locale.RU: "Выберите действие.", Locale.EN: "Choose an action."},
    "panel.btn.tasks_list": {Locale.RU: "📋 Мои задачи", Locale.EN: "📋 My tasks"},
    "panel.btn.search": {Locale.RU: "🔎 Поиск", Locale.EN: "🔎 Search"},
    "panel.btn.memory": {Locale.RU: "🧠 Спросить память", Locale.EN: "🧠 Ask memory"},
    "panel.btn.projects": {Locale.RU: "📁 Проекты", Locale.EN: "📁 Projects"},
    "panel.btn.capture_note": {Locale.RU: "📝 Заметка", Locale.EN: "📝 Note"},
    "panel.btn.capture_task": {Locale.RU: "✅ Задача", Locale.EN: "✅ Task"},
    "panel.btn.capture_idea": {Locale.RU: "💡 Идея", Locale.EN: "💡 Idea"},
    "panel.btn.capture_decision": {Locale.RU: "⚖️ Решение", Locale.EN: "⚖️ Decision"},
    "panel.btn.capture_question": {Locale.RU: "❓ Вопрос", Locale.EN: "❓ Question"},
    "panel.btn.capture_cancel": {Locale.RU: "Отмена", Locale.EN: "Cancel"},
    # Двуязычная кнопка — намеренно одинакова в обоих языках.
    "panel.btn.lang_menu": {
        Locale.RU: "🌐 Язык / Language",
        Locale.EN: "🌐 Язык / Language",
    },
    # Кнопка приглашения — видна только админу (см. panel_button_rows).
    "panel.btn.invite": {Locale.RU: "➕ Пригласить", Locale.EN: "➕ Invite"},
    # --- приглашение ---
    # M6: bearer-токен, срабатывает у первого открывшего; никаких обещаний
    # привязки к конкретному Telegram-аккаунту.
    "invite.message": {
        Locale.RU: (
            "🔗 Ссылка-приглашение (одноразовая, 24 часа):\n\n{link}\n\n"
            "Сработает у того, кто откроет её первым — передавайте только "
            "нужному человеку."
        ),
        Locale.EN: (
            "🔗 Invitation link (one-time, 24 hours):\n\n{link}\n\n"
            "It works for whoever opens it first — share it only with the "
            "intended person."
        ),
    },
    # --- голос ---
    "voice_queued": {
        Locale.RU: "🎙️ Голос сохранён. Расшифровываю…",
        Locale.EN: "🎙️ Voice saved. Transcribing…",
    },
    # --- панель задач ---
    "task_panel.header": {Locale.RU: "📋 Открытые задачи", Locale.EN: "📋 Open tasks"},
    "task_panel.empty": {
        Locale.RU: "📋 Открытых задач нет.",
        Locale.EN: "📋 No open tasks.",
    },
    "task_panel.completed_ok": {Locale.RU: "✅ Выполнено.", Locale.EN: "✅ Done."},
    "task_panel.completed_fail": {
        Locale.RU: "Задача уже закрыта или недоступна.",
        Locale.EN: "This task is already closed or unavailable.",
    },
    # --- поиск: промпт ---
    "search_prompt.required": {
        Locale.RU: "Напишите слово или фразу.",
        Locale.EN: "Type a word or phrase.",
    },
    "search_prompt.intro": {
        Locale.RU: (
            "🔎 Что найти?\n\n"
            "Отправьте слово или фразу. Следующее сообщение станет запросом, "
            "а не новой записью."
        ),
        Locale.EN: (
            "🔎 What to find?\n\n"
            "Send a word or phrase. Your next message becomes the query, "
            "not a new entry."
        ),
    },
    "search_prompt.cancel_btn": {Locale.RU: "✖️ Отмена", Locale.EN: "✖️ Cancel"},
    "search_cancelled": {
        Locale.RU: "✖️ Поиск отменён.",
        Locale.EN: "✖️ Search cancelled.",
    },
    # --- память: промпт ---
    "memory_prompt.required": {
        Locale.RU: "Напишите вопрос.",
        Locale.EN: "Type a question.",
    },
    "memory_prompt.intro": {
        Locale.RU: (
            "🧠 Что спросить у памяти?\n\n"
            "Следующее сообщение станет вопросом к памяти, а не новой записью."
        ),
        Locale.EN: (
            "🧠 What to ask memory?\n\n"
            "Your next message becomes a question to memory, not a new entry."
        ),
    },
    "memory_prompt.cancel_btn": {Locale.RU: "✖️ Отмена", Locale.EN: "✖️ Cancel"},
    "memory_cancelled": {
        Locale.RU: "✖️ Вопрос к памяти отменён.",
        Locale.EN: "✖️ Memory question cancelled.",
    },
    # --- панель поиска ---
    "search_panel.found": {
        Locale.RU: "🔎 Найдено: {count}",
        Locale.EN: "🔎 Found: {count}",
    },
    "search_panel.empty": {
        Locale.RU: (
            "🔎 Ничего не найдено.\n\nПопробуйте другое слово или более короткую фразу."
        ),
        Locale.EN: "🔎 Nothing found.\n\nTry another word or a shorter phrase.",
    },
    "search_panel.again_btn": {
        Locale.RU: "🔎 Искать ещё",
        Locale.EN: "🔎 Search again",
    },
    # --- подписи результатов поиска ---
    "search_label.task_completed": {
        Locale.RU: "☑️ Завершённая задача",
        Locale.EN: "☑️ Completed task",
    },
    "search_label.task": {Locale.RU: "✅ Задача", Locale.EN: "✅ Task"},
    "search_label.note": {Locale.RU: "📝 Заметка", Locale.EN: "📝 Note"},
    "search_label.idea": {Locale.RU: "💡 Идея", Locale.EN: "💡 Idea"},
    "search_label.decision": {Locale.RU: "⚖️ Решение", Locale.EN: "⚖️ Decision"},
    "search_label.question": {Locale.RU: "❓ Вопрос", Locale.EN: "❓ Question"},
    # --- показ записи целиком ---
    # Заголовок открытой записи: метка типа + дата в tz пространства.
    "record_view.header": {
        Locale.RU: "{label} · {date}",
        Locale.EN: "{label} · {date}",
    },
    "record_view.related_header": {
        Locale.RU: "🧬 Похожее по смыслу:",
        Locale.EN: "🧬 Similar in meaning:",
    },
    # --- проекты: промпт названия ---
    "project_name_prompt.required": {
        Locale.RU: "Название не может быть пустым. Напишите название проекта.",
        Locale.EN: "The name cannot be empty. Type a project name.",
    },
    "project_name_prompt.intro": {
        Locale.RU: (
            "📁 Напишите название проекта.\n\n"
            "Следующее сообщение станет названием, а не новой записью."
        ),
        Locale.EN: (
            "📁 Type a project name.\n\n"
            "Your next message becomes the name, not a new entry."
        ),
    },
    "project_name_prompt.cancel_btn": {Locale.RU: "✖️ Отмена", Locale.EN: "✖️ Cancel"},
    # --- панель проектов ---
    "project_panel.not_selected": {Locale.RU: "не выбран", Locale.EN: "none"},
    "project_panel.body": {
        Locale.RU: "📁 Проекты\n\nТекущий: {name}",
        Locale.EN: "📁 Projects\n\nCurrent: {name}",
    },
    "project_panel.new_btn": {
        Locale.RU: "➕ Новый проект",
        Locale.EN: "➕ New project",
    },
    "project_panel.clear_btn": {
        Locale.RU: "✖️ Без проекта",
        Locale.EN: "✖️ No project",
    },
    # --- анонсы проектов ---
    "project_announcement.created": {
        Locale.RU: "✅ Проект выбран.",
        Locale.EN: "✅ Project selected.",
    },
    "project_announcement.selected_ok": {
        Locale.RU: "✅ Текущий проект изменён.",
        Locale.EN: "✅ Current project changed.",
    },
    "project_announcement.selected_fail": {
        Locale.RU: "Проект недоступен. Контекст не изменён.",
        Locale.EN: "Project unavailable. Context unchanged.",
    },
    "project_announcement.cleared_ok": {
        Locale.RU: "✅ Контекст проекта очищен.",
        Locale.EN: "✅ Project context cleared.",
    },
    "project_announcement.cleared_fail": {
        Locale.RU: "Проект уже не выбран.",
        Locale.EN: "No project was selected.",
    },
    # --- ack'и входа (RU — нормальный русский, EN — английский) ---
    "ack.enrolled": {
        Locale.RU: "Готово, доступ открыт.",
        Locale.EN: "Enrollment complete.",
    },
    "ack.enrollment_rejected": {
        Locale.RU: "Не удалось открыть доступ.",
        Locale.EN: "Enrollment could not be completed.",
    },
    "ack.known_user_started": {
        Locale.RU: "С возвращением.",
        Locale.EN: "Welcome back.",
    },
    "ack.memory_question_queued": {
        Locale.RU: "⏳ Готовлю ответ…",
        Locale.EN: "⏳ Preparing the answer…",
    },
    # --- выбор/смена языка ---
    # Chooser двуязычен по своей природе (язык ещё не выбран) — RU==EN намеренно.
    "language.chooser": {
        Locale.RU: "Выберите язык / Choose language",
        Locale.EN: "Выберите язык / Choose language",
    },
    "language.btn.ru": {Locale.RU: "Русский", Locale.EN: "Русский"},
    "language.btn.en": {Locale.RU: "English", Locale.EN: "English"},
    # Подтверждение выбора рендерится уже на ВЫБРАННОМ языке.
    "language.selected": {
        Locale.RU: "Готово. Язык переключён на русский.",
        Locale.EN: "Done. Language set to English.",
    },
    # --- напоминания ---
    # Подтверждение при создании задачи со временем: момент уже в tz пространства.
    "reminder.set": {
        Locale.RU: "⏰ Напомню {when}",
        Locale.EN: "⏰ Reminder set for {when}",
    },
    # Само напоминание в назначенный момент; text = заголовок задачи.
    "reminder.delivered": {
        Locale.RU: "⏰ Напоминание: {text}",
        Locale.EN: "⏰ Reminder: {text}",
    },
    # --- контакты ---
    # Ack на карточку контакта: имя — transient-payload (в receipt не хранится).
    "contact.saved": {
        Locale.RU: "📇 Контакт сохранён: {name}",
        Locale.EN: "📇 Contact saved: {name}",
    },
    # --- selection-feedback (по callback_data) ---
    "selection.capture:note": {Locale.RU: "📝 Заметка", Locale.EN: "📝 Note"},
    "selection.capture:task": {Locale.RU: "✅ Задача", Locale.EN: "✅ Task"},
    "selection.capture:idea": {Locale.RU: "💡 Идея", Locale.EN: "💡 Idea"},
    "selection.capture:decision": {Locale.RU: "⚖️ Решение", Locale.EN: "⚖️ Decision"},
    "selection.capture:question": {Locale.RU: "❓ Вопрос", Locale.EN: "❓ Question"},
    "selection.capture:cancel": {Locale.RU: "✖️ Отменено", Locale.EN: "✖️ Cancelled"},
    "selection.task:await_text": {Locale.RU: "✅ Задача", Locale.EN: "✅ Task"},
    "selection.task:cancel": {Locale.RU: "✖️ Отменено", Locale.EN: "✖️ Cancelled"},
}


# Ack'и, которые доходят до пользователя через send_acknowledgement.
USER_ACKNOWLEDGEMENT_KINDS: tuple[AcknowledgementKind, ...] = (
    AcknowledgementKind.ENROLLED,
    AcknowledgementKind.ENROLLMENT_REJECTED,
    AcknowledgementKind.KNOWN_USER_STARTED,
    AcknowledgementKind.MEMORY_QUESTION_QUEUED,
)

_ACK_KEYS: dict[AcknowledgementKind, str] = {
    AcknowledgementKind.ENROLLED: "ack.enrolled",
    AcknowledgementKind.ENROLLMENT_REJECTED: "ack.enrollment_rejected",
    AcknowledgementKind.KNOWN_USER_STARTED: "ack.known_user_started",
    AcknowledgementKind.MEMORY_QUESTION_QUEUED: "ack.memory_question_queued",
}

# Callback'и, для которых показывается selection-feedback.
SELECTION_CALLBACKS: tuple[str, ...] = (
    "capture:note",
    "capture:task",
    "capture:idea",
    "capture:decision",
    "capture:question",
    "capture:cancel",
    "task:await_text",
    "task:cancel",
)


def _text(key: str, locale: Locale) -> str:
    return CATALOG[key][locale]


# --- панель ---


def panel_text(locale: Locale) -> str:
    return _text("panel.prompt", locale)


def panel_button_rows(
    locale: Locale, is_admin: bool = False
) -> list[list[tuple[str, str]]]:
    """Строки панели как ряды пар (подпись, callback_data).

    Ряд «➕ Пригласить» добавляется ТОЛЬКО админу (member его не видит). Скрытие
    кнопки — не авторизация: сервер заново проверяет роль на invite:create.
    """
    rows = [
        [
            (_text("panel.btn.tasks_list", locale), "tasks:list"),
            (_text("panel.btn.search", locale), "search:prompt"),
            (_text("panel.btn.memory", locale), "memory:ask"),
            (_text("panel.btn.projects", locale), "projects:list"),
        ],
        [
            (_text("panel.btn.capture_note", locale), "capture:note"),
            (_text("panel.btn.capture_task", locale), "capture:task"),
            (_text("panel.btn.capture_idea", locale), "capture:idea"),
        ],
        [
            (_text("panel.btn.capture_decision", locale), "capture:decision"),
            (_text("panel.btn.capture_question", locale), "capture:question"),
            (_text("panel.btn.capture_cancel", locale), "capture:cancel"),
        ],
        [
            (_text("panel.btn.lang_menu", locale), "lang:menu"),
        ],
    ]
    if is_admin:
        rows.append([(_text("panel.btn.invite", locale), "invite:create")])
    return rows


def invite_message_text(link: str, locale: Locale) -> str:
    return _text("invite.message", locale).format(link=link)


# --- голос ---


def voice_queued_text(locale: Locale) -> str:
    return _text("voice_queued", locale)


# --- панель задач ---


def task_panel_header(locale: Locale) -> str:
    return _text("task_panel.header", locale)


def task_panel_empty(locale: Locale) -> str:
    return _text("task_panel.empty", locale)


def task_completion_text(completion_changed: bool | None, locale: Locale) -> str:
    key = (
        "task_panel.completed_ok"
        if completion_changed is True
        else ("task_panel.completed_fail")
    )
    return _text(key, locale)


# --- поиск ---


def search_prompt_text(query_required: bool, locale: Locale) -> str:
    key = "search_prompt.required" if query_required else "search_prompt.intro"
    return _text(key, locale)


def search_cancel_button(locale: Locale) -> str:
    return _text("search_prompt.cancel_btn", locale)


def search_cancelled_text(locale: Locale) -> str:
    return _text("search_cancelled", locale)


def search_panel_found_header(count: int, locale: Locale) -> str:
    return _text("search_panel.found", locale).format(count=count)


def search_panel_empty(locale: Locale) -> str:
    return _text("search_panel.empty", locale)


def search_again_button(locale: Locale) -> str:
    return _text("search_panel.again_btn", locale)


def search_label(record: SearchRecord, locale: Locale) -> str:
    return record_label(record.record_type, record.task_completed, locale)


def record_label(
    record_type: SearchRecordType, task_completed: bool | None, locale: Locale
) -> str:
    if record_type is SearchRecordType.TASK:
        key = "search_label.task_completed" if task_completed else ("search_label.task")
        return _text(key, locale)
    return _text(f"search_label.{record_type.value}", locale)


# --- показ записи целиком ---


def record_view_header(label: str, date: str, locale: Locale) -> str:
    return _text("record_view.header", locale).format(label=label, date=date)


def related_section_header(locale: Locale) -> str:
    return _text("record_view.related_header", locale)


# --- память ---


def memory_prompt_text(question_required: bool, locale: Locale) -> str:
    key = "memory_prompt.required" if question_required else "memory_prompt.intro"
    return _text(key, locale)


def memory_cancel_button(locale: Locale) -> str:
    return _text("memory_prompt.cancel_btn", locale)


def memory_cancelled_text(locale: Locale) -> str:
    return _text("memory_cancelled", locale)


# --- проекты ---


def project_name_prompt_text(name_required: bool, locale: Locale) -> str:
    key = (
        "project_name_prompt.required" if name_required else "project_name_prompt.intro"
    )
    return _text(key, locale)


def project_name_cancel_button(locale: Locale) -> str:
    return _text("project_name_prompt.cancel_btn", locale)


def project_not_selected(locale: Locale) -> str:
    return _text("project_panel.not_selected", locale)


def project_panel_body(current_name: str, locale: Locale) -> str:
    return _text("project_panel.body", locale).format(name=current_name)


def project_new_button(locale: Locale) -> str:
    return _text("project_panel.new_btn", locale)


def project_clear_button(locale: Locale) -> str:
    return _text("project_panel.clear_btn", locale)


def project_announcement(
    kind: AcknowledgementKind, action_succeeded: bool | None, locale: Locale
) -> str | None:
    if kind is AcknowledgementKind.PROJECT_CREATED:
        return _text("project_announcement.created", locale)
    if kind is AcknowledgementKind.PROJECT_SELECTED:
        key = (
            "project_announcement.selected_ok"
            if action_succeeded
            else "project_announcement.selected_fail"
        )
        return _text(key, locale)
    if kind is AcknowledgementKind.PROJECT_CLEARED:
        key = (
            "project_announcement.cleared_ok"
            if action_succeeded
            else "project_announcement.cleared_fail"
        )
        return _text(key, locale)
    return None


# --- напоминания ---


def reminder_set_text(when: str, locale: Locale) -> str:
    return _text("reminder.set", locale).format(when=when)


def reminder_delivered_text(text: str, locale: Locale) -> str:
    return _text("reminder.delivered", locale).format(text=text)


# --- контакты ---


def contact_saved_text(name: str, locale: Locale) -> str:
    return _text("contact.saved", locale).format(name=name)


# --- ack'и и selection-feedback ---


def acknowledgement_text(kind: AcknowledgementKind, locale: Locale) -> str:
    return _text(_ACK_KEYS[kind], locale)


def selection_feedback_text(callback_data: str | None, locale: Locale) -> str | None:
    if callback_data is None:
        return None
    key = f"selection.{callback_data}"
    translations = CATALOG.get(key)
    if translations is None:
        return None
    return translations[locale]


# --- выбор/смена языка ---


def language_chooser_text() -> str:
    # Двуязычный — locale не нужен (язык ещё не выбран).
    return CATALOG["language.chooser"][Locale.RU]


def language_button_ru() -> str:
    return CATALOG["language.btn.ru"][Locale.RU]


def language_button_en() -> str:
    return CATALOG["language.btn.en"][Locale.RU]


def language_selected_text(locale: Locale) -> str:
    return _text("language.selected", locale)

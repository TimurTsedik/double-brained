from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from second_brain.shared.i18n import DEFAULT_LOCALE, Locale
from second_brain.slices.capture.application.contracts import (
    TelegramLink,
    TelegramVoiceMetadata,
)
from second_brain.slices.contacts.application.contracts import TelegramContactPayload
from second_brain.slices.identity.adapters.telegram import messages
from second_brain.slices.identity.application.contracts import (
    LocaleResolver,
    PanelContextResolver,
)
from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.projects.application.contracts import ProjectPanelResult
from second_brain.slices.retrieval.application.contracts import (
    DigestPage,
    DigestPeriod,
    RecordView,
    RecordViewResult,
    SearchPanelResult,
    SearchRecord,
)
from second_brain.slices.tasks.application.contracts import TaskPanelResult
from second_brain.slices.weblinks.application.contracts import RecordLinkView

MAX_TASK_TITLE_LENGTH = 160
MAX_SEARCH_EXCERPT_LENGTH = 240
MAX_PROJECT_BUTTON_LENGTH = 48
MAX_PROJECT_DISPLAY_LENGTH = 160
REMINDER_WHEN_FORMAT = "%d.%m.%Y %H:%M"
RECORD_VIEW_DATE_FORMAT = "%d.%m.%Y"
# Telegram-лимит одного сообщения; сплит считает ВЕСЬ исходящий текст.
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
SHOW_BUTTONS_PER_ROW = 5


class AiogramGateway:
    """Direct, deliberately narrow aiogram wrapper for local polling."""

    def __init__(
        self,
        bot: Bot,
        bot_id: int,
        locale_resolver: LocaleResolver,
        panel_context_resolver: PanelContextResolver | None = None,
    ) -> None:
        self._bot = bot
        self.bot_id = bot_id
        self._locale_resolver = locale_resolver
        self._panel_context_resolver = panel_context_resolver

    async def _resolve_locale(self, update: TelegramUpdate) -> Locale:
        # Единый DB-read в момент построения сообщения: свежий и дублирующий
        # пути дают один и тот же корректный язык (решение 5).
        if update.telegram_user_id is None:
            return DEFAULT_LOCALE
        return await self._locale_resolver.resolve_for_telegram_user(
            update.telegram_user_id
        )

    async def configured_webhook_url(self) -> str | None:
        webhook = await self._bot.get_webhook_info()
        return webhook.url or None

    async def get_updates(
        self, offset: int | None, allowed_updates: list[str]
    ) -> list[TelegramUpdate]:
        updates = await self._bot.get_updates(
            offset=offset,
            allowed_updates=allowed_updates,
        )
        return [self._normalize(update) for update in updates]

    async def send_acknowledgement(
        self, update: TelegramUpdate, kind: AcknowledgementKind
    ) -> None:
        if (
            kind in {AcknowledgementKind.IGNORED, AcknowledgementKind.CAPTURED}
            or not update.is_private
            or update.telegram_user_id is None
        ):
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.acknowledgement_text(kind, locale),
        )

    async def send_panel(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        if self._panel_context_resolver is None:
            raise RuntimeError("send_panel requires a panel context resolver")
        # Панель резолвит locale И is_admin ОДНИМ round-trip'ом (оба поля лежат на
        # одном join-пути TelegramIdentity→User→UserSpace).
        panel_context = await self._panel_context_resolver.resolve_panel_context(
            update.telegram_user_id
        )
        locale = panel_context.locale
        is_admin = panel_context.is_admin
        inline_keyboard = [
            [
                InlineKeyboardButton(text=label, callback_data=callback_data)
                for label, callback_data in row
            ]
            for row in messages.panel_button_rows(locale, is_admin)
        ]
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.panel_text(locale),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_keyboard),
        )

    async def send_invite_link(self, update: TelegramUpdate, link: str) -> None:
        # Ссылка уходит В ЛИЧКУ самому админу (это ок) — приватный чат, известный
        # актёр. Токен в тексте, но нигде не логируется и не хранится.
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.invite_message_text(link, locale),
        )

    async def send_contact_saved(self, update: TelegramUpdate, name: str) -> None:
        # Имя — PII: уходит только в личку сохранившему, нигде не логируется.
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.contact_saved_text(name, locale),
        )

    async def send_selection_feedback(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        text = messages.selection_feedback_text(update.callback_data, locale)
        if text is None:
            return
        await self._bot.send_message(chat_id=update.telegram_user_id, text=text)

    async def send_reminder_set(self, update: TelegramUpdate, when: datetime) -> None:
        # `when` уже в часовом поясе пространства (aware) — здесь только формат
        # и локализованная обёртка «⏰ Напомню …».
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.reminder_set_text(
                when.strftime(REMINDER_WHEN_FORMAT), locale
            ),
        )

    async def send_voice_queued(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.voice_queued_text(locale),
        )

    async def send_task_panel(
        self,
        update: TelegramUpdate,
        result: TaskPanelResult,
        is_completion: bool,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)

        if result.items:
            task_text = (
                messages.task_panel_header(locale)
                + "\n\n"
                + "\n".join(
                    f"{number}. {_truncate_title(item.title)}"
                    for number, item in enumerate(result.items, start=1)
                )
            )
        else:
            task_text = messages.task_panel_empty(locale)

        if is_completion:
            outcome = messages.task_completion_text(result.completion_changed, locale)
            task_text = f"{outcome}\n\n{task_text}"

        if result.items:
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"✅ {number}",
                            callback_data=f"tasks:complete:{item.id}",
                        )
                    ]
                    for number, item in enumerate(result.items, start=1)
                ]
            )
            await self._bot.send_message(
                chat_id=update.telegram_user_id,
                text=task_text,
                reply_markup=reply_markup,
            )
            return
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=task_text,
        )

    async def send_search_prompt(
        self,
        update: TelegramUpdate,
        query_required: bool,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.search_prompt_text(query_required, locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=messages.search_cancel_button(locale),
                            callback_data="search:cancel",
                        )
                    ]
                ]
            ),
        )

    async def send_search_cancelled(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.search_cancelled_text(locale),
        )

    async def send_memory_prompt(
        self,
        update: TelegramUpdate,
        question_required: bool,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.memory_prompt_text(question_required, locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=messages.memory_cancel_button(locale),
                            callback_data="memory:cancel",
                        )
                    ]
                ]
            ),
        )

    async def send_memory_cancelled(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.memory_cancelled_text(locale),
        )

    async def send_search_panel(
        self,
        update: TelegramUpdate,
        result: SearchPanelResult,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        if result.items:
            blocks = [
                f"{number}. {messages.search_label(item, locale)}\n"
                f"{_search_excerpt(item.text)}"
                for number, item in enumerate(result.items, start=1)
            ]
            text = (
                messages.search_panel_found_header(len(result.items), locale)
                + "\n\n"
                + "\n\n".join(blocks)
            )
        else:
            text = messages.search_panel_empty(locale)
        # Номерные кнопки «1…N» открывают запись целиком (show:тип:uuid) — для
        # ВСЕХ результатов: открытие даёт ещё и «похожее», это ценно всегда.
        keyboard_rows = _show_button_rows(result.items)
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=messages.search_again_button(locale),
                    callback_data="search:prompt",
                )
            ]
        )
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def send_record_view(
        self, update: TelegramUpdate, result: RecordViewResult
    ) -> None:
        # Полный текст записи уходит лично вызывающему и нигде не логируется.
        # Сплит считает весь исходящий текст против лимита; блок ссылок — ПОД
        # текстом (текст выше — дословный), секция «похожего» и её кнопки —
        # только на последней части.
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        record = result.record
        header = messages.record_view_header(
            messages.record_label(record.record_type, record.task_completed, locale),
            record.created_at.strftime(RECORD_VIEW_DATE_FORMAT),
            locale,
        )
        parts = _split_outgoing_text(f"{header}\n\n{record.text}")
        links_section = _links_section(result.links, locale)
        if links_section is not None:
            combined = f"{parts[-1]}\n\n{links_section}"
            if len(combined) <= MAX_TELEGRAM_MESSAGE_LENGTH:
                parts[-1] = combined
            else:
                parts.extend(_split_outgoing_text(links_section))
        related_section = _related_section(result.related, locale)
        if related_section is not None:
            combined = f"{parts[-1]}\n\n{related_section}"
            if len(combined) <= MAX_TELEGRAM_MESSAGE_LENGTH:
                parts[-1] = combined
            else:
                parts.append(related_section)
        for part in parts[:-1]:
            await self._bot.send_message(chat_id=update.telegram_user_id, text=part)
        if result.related:
            await self._bot.send_message(
                chat_id=update.telegram_user_id,
                text=parts[-1],
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=_show_button_rows(result.related)
                ),
            )
            return
        await self._bot.send_message(chat_id=update.telegram_user_id, text=parts[-1])

    async def send_digest_menu(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.digest_menu_prompt(locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=messages.digest_period_label(period, locale),
                            callback_data=f"digest:{period.value}",
                        )
                        for period in DigestPeriod
                    ]
                ]
            ),
        )

    async def send_digest(self, update: TelegramUpdate, result: DigestPage) -> None:
        # Тексты записей уходят лично вызывающему и нигде не логируются. Бюджет
        # 4096 считается по ВСЕМУ сообщению; при превышении страница ужимается
        # ЦЕЛЫМИ строками (текст записи не режется посреди слова), а offset
        # следующей страницы равен числу ФАКТИЧЕСКИ отрендеренных строк.
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        if result.total == 0:
            await self._bot.send_message(
                chat_id=update.telegram_user_id,
                text=messages.digest_empty_text(result.period, locale),
            )
            return
        header = messages.digest_header(
            messages.digest_period_label(result.period, locale),
            result.period_start.strftime(RECORD_VIEW_DATE_FORMAT),
            result.as_of.strftime(RECORD_VIEW_DATE_FORMAT),
            locale,
        )
        counters_line = messages.digest_counters_line(result.counters, locale)
        rows = [
            messages.digest_row(
                result.offset + position,
                messages.record_label(item.record_type, item.task_completed, locale),
                item.created_at.strftime(RECORD_VIEW_DATE_FORMAT),
                _search_excerpt(item.text),
                locale,
            )
            for position, item in enumerate(result.items, start=1)
        ]

        def build_text(row_count: int) -> str:
            return f"{header}\n{counters_line}\n\n" + "\n".join(rows[:row_count])

        # Одна строка (фрагмент ≤240) в лимит влезает всегда — ниже 1 не ужимаем.
        rendered = len(rows)
        while rendered > 1 and len(build_text(rendered)) > (
            MAX_TELEGRAM_MESSAGE_LENGTH
        ):
            rendered -= 1
        keyboard_rows = _show_button_rows(
            result.items[:rendered], start=result.offset + 1
        )
        next_offset = result.offset + rendered
        if next_offset < result.total:
            as_of_unix = int(result.as_of.timestamp())
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=messages.digest_more_button(locale),
                        callback_data=(
                            f"digest:more:{result.period.value}:"
                            f"{next_offset}:{as_of_unix}"
                        ),
                    )
                ]
            )
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=build_text(rendered),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def send_project_name_prompt(
        self,
        update: TelegramUpdate,
        name_required: bool,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.project_name_prompt_text(name_required, locale),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=messages.project_name_cancel_button(locale),
                            callback_data="projects:list",
                        )
                    ]
                ]
            ),
        )

    async def send_project_panel(
        self,
        update: TelegramUpdate,
        result: ProjectPanelResult,
        kind: AcknowledgementKind,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        current = next(
            (item for item in result.items if item.id == result.current_project_id),
            None,
        )
        current_name = (
            messages.project_not_selected(locale)
            if current is None
            else _truncate_project_name(current.name, MAX_PROJECT_DISPLAY_LENGTH)
        )
        announcement = messages.project_announcement(
            kind, result.action_succeeded, locale
        )
        panel_text = messages.project_panel_body(current_name, locale)
        if announcement is not None:
            panel_text = f"{announcement}\n\n{panel_text}"
        project_rows = [
            [
                InlineKeyboardButton(
                    text=_project_button_text(
                        item.name, item.id == result.current_project_id
                    ),
                    callback_data=f"projects:select:{item.id}",
                )
            ]
            for item in result.items
        ]
        project_rows.append(
            [
                InlineKeyboardButton(
                    text=messages.project_new_button(locale),
                    callback_data="projects:create",
                ),
                InlineKeyboardButton(
                    text=messages.project_clear_button(locale),
                    callback_data="projects:clear",
                ),
            ]
        )
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=panel_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=project_rows),
        )

    async def send_language_prompt(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        # Chooser двуязычен (язык ещё не выбран) — locale не резолвим.
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.language_chooser_text(),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=messages.language_button_ru(),
                            callback_data="lang:ru",
                        ),
                        InlineKeyboardButton(
                            text=messages.language_button_en(),
                            callback_data="lang:en",
                        ),
                    ]
                ]
            ),
        )

    async def send_language_selected(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        # Язык уже записан и закоммичен в ingress-транзакции → резолвим НОВЫЙ.
        locale = await self._resolve_locale(update)
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.language_selected_text(locale),
        )

    async def answer_callback(self, update: TelegramUpdate) -> None:
        if update.callback_query_id is None:
            return
        await self._bot.answer_callback_query(update.callback_query_id)

    def _normalize(self, update: Update) -> TelegramUpdate:
        callback = getattr(update, "callback_query", None)
        if callback is not None:
            message = getattr(callback, "message", None)
            chat = getattr(message, "chat", None)
            actor = callback.from_user.id if callback.from_user is not None else None
            callback_data = callback.data if isinstance(callback.data, str) else None
            return TelegramUpdate(
                bot_id=self.bot_id,
                update_id=update.update_id,
                is_private=getattr(chat, "type", None) == "private",
                telegram_user_id=actor,
                text=None,
                callback_query_id=callback.id,
                callback_data=callback_data,
            )
        message = getattr(update, "message", None)
        if message is None:
            return TelegramUpdate(
                bot_id=self.bot_id,
                update_id=update.update_id,
                is_private=False,
                telegram_user_id=None,
                text=None,
            )

        actor = message.from_user.id if message.from_user is not None else None
        voice = getattr(message, "voice", None)
        voice_metadata = None
        if voice is not None:
            voice_metadata = TelegramVoiceMetadata(
                file_id=voice.file_id,
                file_unique_id=voice.file_unique_id,
                duration_seconds=voice.duration,
                file_size=voice.file_size,
                mime_type=voice.mime_type,
            )
        contact = getattr(message, "contact", None)
        contact_payload = None
        if contact is not None:
            # Только payload карточки (PII, repr-hidden). contact.user_id
            # НЕ переносим: маршрутизация всегда по отправителю (from_user).
            contact_payload = TelegramContactPayload(
                phone_number=contact.phone_number,
                first_name=contact.first_name,
                last_name=contact.last_name,
            )
        message_text = message.text if isinstance(message.text, str) else None
        return TelegramUpdate(
            bot_id=self.bot_id,
            update_id=update.update_id,
            is_private=message.chat.type == "private",
            telegram_user_id=actor,
            text=message_text,
            telegram_message_id=message.message_id,
            voice=voice_metadata,
            contact=contact_payload,
            # S1: только message.entities (caption придёт со слайсом картинок).
            links=_extract_links(message_text, getattr(message, "entities", None)),
        )


def _utf16_offset_to_index(text: str, utf16_offset: int) -> int:
    """Перевод UTF-16 code-unit смещения Telegram в индекс Python-строки.

    Offsets/length у Telegram-entities считаются в UTF-16 юнитах: символ вне
    BMP (эмодзи) занимает ДВА юнита, но ОДИН символ Python-строки. Прямое
    индексирование уводило бы label влево на каждом эмодзи перед ссылкой.
    """
    units = 0
    for index, char in enumerate(text):
        if units >= utf16_offset:
            return index
        units += 1 if ord(char) < 0x10000 else 2
    return len(text)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    start = _utf16_offset_to_index(text, offset)
    end = _utf16_offset_to_index(text, offset + length)
    return text[start:end]


def _extract_links(
    text: str | None, entities: Sequence[Any] | None
) -> tuple[TelegramLink, ...]:
    """Ссылки из message.entities в порядке появления.

    text_link: url спрятан в entity.url, label — накрытая подстрока текста;
    url: голый адрес, label = сам url (= подстрока). Остальные entity-типы
    ссылок не несут и пропускаются.
    """
    if text is None or not entities:
        return ()
    links: list[TelegramLink] = []
    for entity in entities:
        entity_type = getattr(entity, "type", None)
        if entity_type == "text_link":
            url = getattr(entity, "url", None)
            if isinstance(url, str) and url:
                links.append(
                    TelegramLink(
                        label=_utf16_slice(text, entity.offset, entity.length),
                        url=url,
                    )
                )
        elif entity_type == "url":
            bare_url = _utf16_slice(text, entity.offset, entity.length)
            links.append(TelegramLink(label=bare_url, url=bare_url))
    return tuple(links)


def _truncate_title(title: str) -> str:
    if len(title) <= MAX_TASK_TITLE_LENGTH:
        return title
    return f"{title[: MAX_TASK_TITLE_LENGTH - 1]}…"


def _search_excerpt(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= MAX_SEARCH_EXCERPT_LENGTH:
        return compact
    return f"{compact[: MAX_SEARCH_EXCERPT_LENGTH - 1]}…"


def _show_button_rows(
    items: Sequence[SearchRecord | RecordView],
    start: int = 1,
) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(
            text=f"{number}",
            callback_data=f"show:{item.record_type.value}:{item.id}",
        )
        for number, item in enumerate(items, start=start)
    ]
    return [
        buttons[start : start + SHOW_BUTTONS_PER_ROW]
        for start in range(0, len(buttons), SHOW_BUTTONS_PER_ROW)
    ]


def _links_section(links: tuple[RecordLinkView, ...], locale: Locale) -> str | None:
    # Sidecar-блок ссылок под дословным текстом: plain text БЕЗ parse_mode,
    # как весь рендер. Нет ссылок — блока просто нет.
    if not links:
        return None
    lines = [_link_line(link) for link in links]
    return messages.record_links_header(locale) + "\n" + "\n".join(lines)


def _link_line(link: RecordLinkView) -> str:
    # label==url (голый url) не дублируется; title вставляется, когда страница
    # уже fetched: «label — title — url» / «title — url» / «label — url» / «url».
    parts = []
    if link.label != link.url:
        parts.append(link.label)
    if link.title is not None:
        parts.append(link.title)
    parts.append(link.url)
    return " — ".join(parts)


def _related_section(related: tuple[RecordView, ...], locale: Locale) -> str | None:
    # Нет вектора или соседей — секции просто нет, без объяснений.
    if not related:
        return None
    blocks = [
        f"{number}. "
        f"{messages.record_label(item.record_type, item.task_completed, locale)}\n"
        f"{_search_excerpt(item.text)}"
        for number, item in enumerate(related, start=1)
    ]
    return messages.related_section_header(locale) + "\n\n" + "\n\n".join(blocks)


def _split_outgoing_text(text: str) -> list[str]:
    """Режет исходящий текст на части ≤4096 по границе строки/слова.

    Патологический токен длиннее лимита режется жёстко: гарантия доставки
    выше красоты.
    """
    parts: list[str] = []
    remaining = text
    while len(remaining) > MAX_TELEGRAM_MESSAGE_LENGTH:
        window = remaining[: MAX_TELEGRAM_MESSAGE_LENGTH + 1]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < 1:
            parts.append(remaining[:MAX_TELEGRAM_MESSAGE_LENGTH])
            remaining = remaining[MAX_TELEGRAM_MESSAGE_LENGTH:]
            continue
        parts.append(remaining[:cut])
        remaining = remaining[cut + 1 :]
    if remaining:
        parts.append(remaining)
    return parts


def _project_button_text(name: str, current: bool) -> str:
    prefix = "✓ " if current else ""
    available = MAX_PROJECT_BUTTON_LENGTH - len(prefix)
    return f"{prefix}{_truncate_project_name(name, available)}"


def _truncate_project_name(name: str, limit: int) -> str:
    compact = " ".join(name.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}…"

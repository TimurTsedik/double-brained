from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from second_brain.shared.i18n import DEFAULT_LOCALE, Locale
from second_brain.slices.capture.application.contracts import TelegramVoiceMetadata
from second_brain.slices.identity.adapters.telegram import messages
from second_brain.slices.identity.application.contracts import LocaleResolver
from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.projects.application.contracts import ProjectPanelResult
from second_brain.slices.retrieval.application.contracts import SearchPanelResult
from second_brain.slices.tasks.application.contracts import TaskPanelResult

MAX_TASK_TITLE_LENGTH = 160
MAX_SEARCH_EXCERPT_LENGTH = 240
MAX_PROJECT_BUTTON_LENGTH = 48
MAX_PROJECT_DISPLAY_LENGTH = 160


class AiogramGateway:
    """Direct, deliberately narrow aiogram wrapper for local polling."""

    def __init__(self, bot: Bot, bot_id: int, locale_resolver: LocaleResolver) -> None:
        self._bot = bot
        self.bot_id = bot_id
        self._locale_resolver = locale_resolver

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
        locale = await self._resolve_locale(update)
        inline_keyboard = [
            [
                InlineKeyboardButton(text=label, callback_data=callback_data)
                for label, callback_data in row
            ]
            for row in messages.panel_button_rows(locale)
        ]
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=messages.panel_text(locale),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_keyboard),
        )

    async def send_selection_feedback(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        locale = await self._resolve_locale(update)
        text = messages.selection_feedback_text(update.callback_data, locale)
        if text is None:
            return
        await self._bot.send_message(chat_id=update.telegram_user_id, text=text)

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
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=messages.search_again_button(locale),
                            callback_data="search:prompt",
                        )
                    ]
                ]
            ),
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
        return TelegramUpdate(
            bot_id=self.bot_id,
            update_id=update.update_id,
            is_private=message.chat.type == "private",
            telegram_user_id=actor,
            text=message.text if isinstance(message.text, str) else None,
            telegram_message_id=message.message_id,
            voice=voice_metadata,
        )


def _truncate_title(title: str) -> str:
    if len(title) <= MAX_TASK_TITLE_LENGTH:
        return title
    return f"{title[: MAX_TASK_TITLE_LENGTH - 1]}…"


def _search_excerpt(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= MAX_SEARCH_EXCERPT_LENGTH:
        return compact
    return f"{compact[: MAX_SEARCH_EXCERPT_LENGTH - 1]}…"


def _project_button_text(name: str, current: bool) -> str:
    prefix = "✓ " if current else ""
    available = MAX_PROJECT_BUTTON_LENGTH - len(prefix)
    return f"{prefix}{_truncate_project_name(name, available)}"


def _truncate_project_name(name: str, limit: int) -> str:
    compact = " ".join(name.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}…"

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.identity.application.telegram_update import TelegramUpdate
from second_brain.slices.tasks.application.contracts import TaskPanelResult

MAX_TASK_TITLE_LENGTH = 160


class AiogramGateway:
    """Direct, deliberately narrow aiogram wrapper for local polling."""

    def __init__(self, bot: Bot, bot_id: int) -> None:
        self._bot = bot
        self.bot_id = bot_id

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
        await self._bot.send_message(
            chat_id=update.telegram_user_id, text=_acknowledgement_text(kind)
        )

    async def send_panel(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        await self._bot.send_message(
            chat_id=update.telegram_user_id,
            text="Выберите действие.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📋 Мои задачи", callback_data="tasks:list"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📝 Заметка", callback_data="capture:note"
                        ),
                        InlineKeyboardButton(
                            text="✅ Задача", callback_data="capture:task"
                        ),
                        InlineKeyboardButton(
                            text="💡 Идея", callback_data="capture:idea"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚖️ Решение", callback_data="capture:decision"
                        ),
                        InlineKeyboardButton(
                            text="❓ Вопрос", callback_data="capture:question"
                        ),
                        InlineKeyboardButton(
                            text="Отмена", callback_data="capture:cancel"
                        ),
                    ],
                ]
            ),
        )

    async def send_selection_feedback(self, update: TelegramUpdate) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return
        text = _selection_feedback_text(update.callback_data)
        if text is None:
            return
        await self._bot.send_message(chat_id=update.telegram_user_id, text=text)

    async def send_task_panel(
        self,
        update: TelegramUpdate,
        result: TaskPanelResult,
        is_completion: bool,
    ) -> None:
        if not update.is_private or update.telegram_user_id is None:
            return

        if result.items:
            task_text = "📋 Открытые задачи\n\n" + "\n".join(
                f"{number}. {_truncate_title(item.title)}"
                for number, item in enumerate(result.items, start=1)
            )
        else:
            task_text = "📋 Открытых задач нет."

        if is_completion:
            outcome = (
                "✅ Выполнено."
                if result.completion_changed is True
                else "Задача уже закрыта или недоступна."
            )
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
        return TelegramUpdate(
            bot_id=self.bot_id,
            update_id=update.update_id,
            is_private=message.chat.type == "private",
            telegram_user_id=actor,
            text=message.text if isinstance(message.text, str) else None,
            telegram_message_id=message.message_id,
        )


def _acknowledgement_text(kind: AcknowledgementKind) -> str:
    messages = {
        AcknowledgementKind.ENROLLED: "Enrollment complete.",
        AcknowledgementKind.ENROLLMENT_REJECTED: "Enrollment could not be completed.",
        AcknowledgementKind.KNOWN_USER_STARTED: "Welcome back.",
    }
    return messages[kind]


def _selection_feedback_text(callback_data: str | None) -> str | None:
    if callback_data is None:
        return None
    messages = {
        "capture:note": "📝 Заметка",
        "capture:task": "✅ Задача",
        "capture:idea": "💡 Идея",
        "capture:decision": "⚖️ Решение",
        "capture:question": "❓ Вопрос",
        "capture:cancel": "✖️ Отменено",
        "task:await_text": "✅ Задача",
        "task:cancel": "✖️ Отменено",
    }
    return messages.get(callback_data)


def _truncate_title(title: str) -> str:
    if len(title) <= MAX_TASK_TITLE_LENGTH:
        return title
    return f"{title[: MAX_TASK_TITLE_LENGTH - 1]}…"

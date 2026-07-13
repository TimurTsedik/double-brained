from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from second_brain.slices.identity.application.local_updates import AcknowledgementKind
from second_brain.slices.identity.application.telegram_update import TelegramUpdate


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
                    ]
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

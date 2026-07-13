from dataclasses import dataclass, field


@dataclass(frozen=True)
class TelegramUpdate:
    """Normalized Telegram input with text kept transient until trusted routing."""

    bot_id: int
    update_id: int
    is_private: bool
    telegram_user_id: int | None
    text: str | None = field(repr=False)
    telegram_message_id: int | None = None

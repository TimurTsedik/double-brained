from dataclasses import dataclass, field


@dataclass(frozen=True)
class TelegramUpdate:
    """Normalized Telegram input whose text fields must not be persisted."""

    bot_id: int
    update_id: int
    is_private: bool
    telegram_user_id: int | None
    text: str | None = field(repr=False)

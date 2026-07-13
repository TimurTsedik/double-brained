import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    invite_token_pepper: bytes = field(repr=False)
    invite_token_pepper_key_id: str

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = _required_environment("DATABASE_URL")
        telegram_bot_token = _required_environment("TELEGRAM_BOT_TOKEN")
        invite_token_pepper = _required_environment("INVITE_TOKEN_PEPPER").encode()
        invite_token_pepper_key_id = _required_environment("INVITE_TOKEN_PEPPER_KEY_ID")
        return cls(
            database_url=database_url,
            telegram_bot_token=telegram_bot_token,
            invite_token_pepper=invite_token_pepper,
            invite_token_pepper_key_id=invite_token_pepper_key_id,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value

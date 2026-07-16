import os
from dataclasses import dataclass, field

DEFAULT_VOICE_STORAGE_ROOT = ".data/voice"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_PANEL_FOLLOWUP_SECONDS = 5


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    schema_database_url: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    invite_token_pepper: bytes = field(repr=False)
    invite_token_pepper_key_id: str
    voice_storage_root: str = field(
        default=DEFAULT_VOICE_STORAGE_ROOT,
        repr=False,
    )
    whisper_model: str = DEFAULT_WHISPER_MODEL
    open_router_ai_key: str | None = field(default=None, repr=False)
    # Через сколько секунд после действия пользователя дослать панель с
    # кнопками (0 = фича выключена).
    panel_followup_seconds: int = DEFAULT_PANEL_FOLLOWUP_SECONDS

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = _required_environment("DATABASE_URL")
        schema_database_url = _required_environment("SCHEMA_DATABASE_URL")
        if database_url == schema_database_url:
            raise RuntimeError("DATABASE_URL must differ from SCHEMA_DATABASE_URL")
        telegram_bot_token = _required_environment("TELEGRAM_BOT_TOKEN")
        invite_token_pepper = _required_environment("INVITE_TOKEN_PEPPER").encode()
        invite_token_pepper_key_id = _required_environment("INVITE_TOKEN_PEPPER_KEY_ID")
        voice_storage_root = (
            os.environ.get("VOICE_STORAGE_ROOT") or DEFAULT_VOICE_STORAGE_ROOT
        )
        whisper_model = os.environ.get("WHISPER_MODEL") or DEFAULT_WHISPER_MODEL
        open_router_ai_key = os.environ.get("OPEN_ROUTER_AI_KEY") or None
        panel_followup_seconds = _non_negative_int_environment(
            "PANEL_FOLLOWUP_SECONDS", DEFAULT_PANEL_FOLLOWUP_SECONDS
        )
        return cls(
            database_url=database_url,
            schema_database_url=schema_database_url,
            telegram_bot_token=telegram_bot_token,
            invite_token_pepper=invite_token_pepper,
            invite_token_pepper_key_id=invite_token_pepper_key_id,
            voice_storage_root=voice_storage_root,
            whisper_model=whisper_model,
            open_router_ai_key=open_router_ai_key,
            panel_followup_seconds=panel_followup_seconds,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _non_negative_int_environment(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a non-negative integer") from None
    if value < 0:
        raise RuntimeError(f"{name} must be a non-negative integer")
    return value

import os
from dataclasses import dataclass, field

DEFAULT_VOICE_STORAGE_ROOT = ".data/voice"
DEFAULT_MLX_WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"


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
    mlx_whisper_model: str = DEFAULT_MLX_WHISPER_MODEL

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
        mlx_whisper_model = (
            os.environ.get("MLX_WHISPER_MODEL") or DEFAULT_MLX_WHISPER_MODEL
        )
        return cls(
            database_url=database_url,
            schema_database_url=schema_database_url,
            telegram_bot_token=telegram_bot_token,
            invite_token_pepper=invite_token_pepper,
            invite_token_pepper_key_id=invite_token_pepper_key_id,
            voice_storage_root=voice_storage_root,
            mlx_whisper_model=mlx_whisper_model,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value

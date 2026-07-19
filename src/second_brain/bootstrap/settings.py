import os
from dataclasses import dataclass, field

DEFAULT_VOICE_STORAGE_ROOT = ".data/voice"
# Оригиналы фото (S2): отдельный корень и лимит скачиваемого файла.
DEFAULT_IMAGE_STORAGE_ROOT = ".data/images"
DEFAULT_IMAGE_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_PANEL_FOLLOWUP_SECONDS = 5
# Фетч <title> страниц (S1): лимиты SSRF-контракта и модель попыток воркера.
DEFAULT_TITLE_FETCH_TIMEOUT_SECONDS = 5
DEFAULT_TITLE_FETCH_MAX_BYTES = 262_144
DEFAULT_TITLE_FETCH_MAX_REDIRECTS = 3
DEFAULT_TITLE_MAX_LENGTH = 200
DEFAULT_TITLE_FETCH_MAX_ATTEMPTS = 5
DEFAULT_TITLE_FETCH_RETRY_BACKOFF_SECONDS = 60
# Webhook-INBOX (эпик API-1, B1): cap тела запроса и модель попыток шага.
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_INBOX_MAX_ATTEMPTS = 5
DEFAULT_INBOX_RETRY_BACKOFF_SECONDS = 30
# Пороги команды статуса очереди (B4): с какого возраста головы очередь
# считается вставшей и насколько свежей должна быть жалоба Telegram.
DEFAULT_INBOX_HEAD_AGE_ALERT_SECONDS = 300
DEFAULT_INBOX_WEBHOOK_ERROR_WINDOW_SECONDS = 3600


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
    # Хранилище оригиналов фото и потолок размера скачиваемого файла (S2).
    image_storage_root: str = field(
        default=DEFAULT_IMAGE_STORAGE_ROOT,
        repr=False,
    )
    image_max_file_size_bytes: int = DEFAULT_IMAGE_MAX_FILE_SIZE_BYTES
    whisper_model: str = DEFAULT_WHISPER_MODEL
    open_router_ai_key: str | None = field(default=None, repr=False)
    # Через сколько секунд после действия пользователя дослать панель с
    # кнопками (0 = фича выключена).
    panel_followup_seconds: int = DEFAULT_PANEL_FOLLOWUP_SECONDS
    # Фоновый фетч <title> страниц: off → шаг воркера не клеймит вовсе.
    title_fetch_enabled: bool = True
    title_fetch_timeout_seconds: int = DEFAULT_TITLE_FETCH_TIMEOUT_SECONDS
    # Cap на тело ответа — и сжатое, и распакованное (SSRF-контракт §1.2).
    title_fetch_max_bytes: int = DEFAULT_TITLE_FETCH_MAX_BYTES
    title_fetch_max_redirects: int = DEFAULT_TITLE_FETCH_MAX_REDIRECTS
    title_max_length: int = DEFAULT_TITLE_MAX_LENGTH
    # Модель попыток воркера (по образцу reminder-delivery): после
    # max_attempts сбоев строка — failed; между попытками линейный бэкофф.
    title_fetch_max_attempts: int = DEFAULT_TITLE_FETCH_MAX_ATTEMPTS
    title_fetch_retry_backoff_seconds: int = DEFAULT_TITLE_FETCH_RETRY_BACKOFF_SECONDS
    # Webhook (B1): секрет заголовка X-Telegram-Bot-Api-Secret-Token.
    # None/пусто = webhook не сконфигурирован → роут отвечает 503.
    telegram_webhook_secret: str | None = field(default=None, repr=False)
    webhook_max_body_bytes: int = DEFAULT_WEBHOOK_MAX_BODY_BYTES
    # Модель попыток inbox-шага (по образцу title-fetch): после max_attempts
    # сбоев строка — failed; между попытками линейный бэкофф.
    inbox_max_attempts: int = DEFAULT_INBOX_MAX_ATTEMPTS
    inbox_retry_backoff_seconds: int = DEFAULT_INBOX_RETRY_BACKOFF_SECONDS
    # Пороги команды second-brain-inbox-status (B4). Возраст головы, с
    # которого очередь считается вставшей: голова моложе — обычная обработка.
    inbox_head_age_alert_seconds: int = DEFAULT_INBOX_HEAD_AGE_ALERT_SECONDS
    # Окно свежести жалобы Telegram: last_error он НЕ сбрасывает после удачной
    # доставки (только setWebhook), поэтому старая ошибка — история, не авария.
    inbox_webhook_error_window_seconds: int = DEFAULT_INBOX_WEBHOOK_ERROR_WINDOW_SECONDS

    def telegram_bot_id(self) -> int:
        """Числовой id бота из префикса токена (формат Telegram «id:hmac»).

        Нужен webhook-роуту без похода в сеть: get_me() в HTTP-запросе делать
        нельзя, а префикс токена — тот же id, что возвращает get_me().
        """
        prefix = self.telegram_bot_token.split(":", 1)[0]
        if not prefix.isdigit():
            raise RuntimeError("TELEGRAM_BOT_TOKEN must start with the numeric bot id")
        return int(prefix)

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
        image_storage_root = (
            os.environ.get("IMAGE_STORAGE_ROOT") or DEFAULT_IMAGE_STORAGE_ROOT
        )
        image_max_file_size_bytes = _non_negative_int_environment(
            "IMAGE_MAX_FILE_SIZE_BYTES", DEFAULT_IMAGE_MAX_FILE_SIZE_BYTES
        )
        whisper_model = os.environ.get("WHISPER_MODEL") or DEFAULT_WHISPER_MODEL
        open_router_ai_key = os.environ.get("OPEN_ROUTER_AI_KEY") or None
        panel_followup_seconds = _non_negative_int_environment(
            "PANEL_FOLLOWUP_SECONDS", DEFAULT_PANEL_FOLLOWUP_SECONDS
        )
        title_fetch_enabled = _bool_environment("TITLE_FETCH_ENABLED", True)
        title_fetch_timeout_seconds = _non_negative_int_environment(
            "TITLE_FETCH_TIMEOUT_SECONDS", DEFAULT_TITLE_FETCH_TIMEOUT_SECONDS
        )
        title_fetch_max_bytes = _non_negative_int_environment(
            "TITLE_FETCH_MAX_BYTES", DEFAULT_TITLE_FETCH_MAX_BYTES
        )
        title_fetch_max_redirects = _non_negative_int_environment(
            "TITLE_FETCH_MAX_REDIRECTS", DEFAULT_TITLE_FETCH_MAX_REDIRECTS
        )
        title_max_length = _non_negative_int_environment(
            "TITLE_MAX_LENGTH", DEFAULT_TITLE_MAX_LENGTH
        )
        title_fetch_max_attempts = _non_negative_int_environment(
            "TITLE_FETCH_MAX_ATTEMPTS", DEFAULT_TITLE_FETCH_MAX_ATTEMPTS
        )
        title_fetch_retry_backoff_seconds = _non_negative_int_environment(
            "TITLE_FETCH_RETRY_BACKOFF_SECONDS",
            DEFAULT_TITLE_FETCH_RETRY_BACKOFF_SECONDS,
        )
        telegram_webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or None
        webhook_max_body_bytes = _non_negative_int_environment(
            "WEBHOOK_MAX_BODY_BYTES", DEFAULT_WEBHOOK_MAX_BODY_BYTES
        )
        inbox_max_attempts = _non_negative_int_environment(
            "INBOX_MAX_ATTEMPTS", DEFAULT_INBOX_MAX_ATTEMPTS
        )
        inbox_retry_backoff_seconds = _non_negative_int_environment(
            "INBOX_RETRY_BACKOFF_SECONDS", DEFAULT_INBOX_RETRY_BACKOFF_SECONDS
        )
        inbox_head_age_alert_seconds = _non_negative_int_environment(
            "INBOX_HEAD_AGE_ALERT_SECONDS", DEFAULT_INBOX_HEAD_AGE_ALERT_SECONDS
        )
        inbox_webhook_error_window_seconds = _non_negative_int_environment(
            "INBOX_WEBHOOK_ERROR_WINDOW_SECONDS",
            DEFAULT_INBOX_WEBHOOK_ERROR_WINDOW_SECONDS,
        )
        return cls(
            database_url=database_url,
            schema_database_url=schema_database_url,
            telegram_bot_token=telegram_bot_token,
            invite_token_pepper=invite_token_pepper,
            invite_token_pepper_key_id=invite_token_pepper_key_id,
            voice_storage_root=voice_storage_root,
            image_storage_root=image_storage_root,
            image_max_file_size_bytes=image_max_file_size_bytes,
            whisper_model=whisper_model,
            open_router_ai_key=open_router_ai_key,
            panel_followup_seconds=panel_followup_seconds,
            title_fetch_enabled=title_fetch_enabled,
            title_fetch_timeout_seconds=title_fetch_timeout_seconds,
            title_fetch_max_bytes=title_fetch_max_bytes,
            title_fetch_max_redirects=title_fetch_max_redirects,
            title_max_length=title_max_length,
            title_fetch_max_attempts=title_fetch_max_attempts,
            title_fetch_retry_backoff_seconds=title_fetch_retry_backoff_seconds,
            telegram_webhook_secret=telegram_webhook_secret,
            webhook_max_body_bytes=webhook_max_body_bytes,
            inbox_max_attempts=inbox_max_attempts,
            inbox_retry_backoff_seconds=inbox_retry_backoff_seconds,
            inbox_head_age_alert_seconds=inbox_head_age_alert_seconds,
            inbox_webhook_error_window_seconds=inbox_webhook_error_window_seconds,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _bool_environment(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean (on/off)")


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

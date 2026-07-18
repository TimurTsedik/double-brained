from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class TelegramInboxStatus(StrEnum):
    """Статус строки webhook-INBOX: ждёт обработки / обработана / исчерпана."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"

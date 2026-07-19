from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base
from second_brain.slices.identity.domain.entities import TelegramInboxStatus

USER_ROLE_CHECK_NAME = "ck_users_role_admin"
ACTIVE_ADMIN_INDEX_NAME = "uq_users_active_admin"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name=USER_ROLE_CHECK_NAME),
        # Один активный admin на установку (M9): даже при гонке БД не даст второго.
        Index(
            ACTIVE_ADMIN_INDEX_NAME,
            "role",
            unique=True,
            postgresql_where=text("role = 'admin' AND is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


USER_SPACE_LANGUAGE_CHECK_NAME = "ck_user_spaces_language"


class UserSpace(Base):
    __tablename__ = "user_spaces"
    __table_args__ = (
        CheckConstraint(
            "timezone = 'Asia/Jerusalem'",
            name="ck_user_spaces_timezone_asia_jerusalem",
        ),
        CheckConstraint(
            "language IS NULL OR language IN ('ru', 'en')",
            name=USER_SPACE_LANGUAGE_CHECK_NAME,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL = язык ещё не выбран → эффективный RU (forward-only, решение 1 плана).
    language: Mapped[str | None] = mapped_column(String(2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TelegramIdentity(Base):
    __tablename__ = "telegram_identities"
    __table_args__ = (
        Index(
            "uq_telegram_identities_active_telegram_user_id",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "uq_telegram_identities_active_user_id",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


ENROLLMENT_INVITE_ROLE_CHECK_NAME = "ck_enrollment_invites_role_admin"
ENROLLMENT_INVITE_ACTOR_CHECK_NAME = "ck_enrollment_invites_bootstrap_actor"
# Легаси частичный уникальный индекс «один pending» — снимается реконсиляцией.
ENROLLMENT_INVITE_PENDING_INDEX_NAME = "uq_enrollment_invites_pending_bootstrap"


class EnrollmentInvite(Base):
    __tablename__ = "enrollment_invites"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'member')", name=ENROLLMENT_INVITE_ROLE_CHECK_NAME
        ),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'expired', 'revoked')",
            name="ck_enrollment_invites_status",
        ),
        CheckConstraint(
            "created_by_actor IN ('bootstrap_cli', 'admin_bot')",
            name=ENROLLMENT_INVITE_ACTOR_CHECK_NAME,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32), unique=True, nullable=False
    )
    pepper_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class ApiToken(Base):
    """Токен доступа к публичному HTTP-API: в базе — только хэш секрета.

    RLS на таблице НЕТ, и это осознанно, а не забыто. Проверка токена случается
    ДО того, как известно пространство пользователя: именно по токену мы и
    узнаём, кто пришёл, — значит строка обязана читаться вне scope. Ровно та же
    модель, что у enrollment_invites (и по той же причине); изоляция здесь
    держится не политикой БД, а предикатом user_id в каждом запросе.

    Перец у токенов СВОЙ (api_tokens.pepper_key_id ≠ перец инвайтов): ротация
    перца инвайтов не должна разлогинивать все выданные API-токены — это разные
    жизненные циклы. Поиск, как у инвайтов, идёт по паре (хэш, pepper_key_id).

    Отзыв — это ПОМЕТКА revoked_at, а не удаление: история выданного доступа
    остаётся (и DELETE роли приложения не выдан).
    """

    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32), unique=True, nullable=False
    )
    pepper_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Человекочитаемая метка, чтобы владелец различал свои токены в списке.
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # NULL = токеном ещё не пользовались. Пишется НЕ на каждый запрос API —
    # см. AuthenticateApiToken (окно троттлинга).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


RESULT_KIND_CHECK_NAME = "ck_telegram_update_receipts_result_kind"


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipts"
    __table_args__ = (
        CheckConstraint(
            "result_kind IN "
            "('captured', 'enrolled', 'enrollment_rejected', "
            "'known_user_started', 'panel_shown', 'task_mode_set', "
            "'task_mode_cancelled', 'tasks_listed', 'task_completed', "
            "'search_mode_set', 'search_mode_cancelled', "
            "'search_query_required', 'search_completed', 'record_shown', "
            "'edit_mode_set', 'edit_mode_cancelled', 'record_edited', "
            "'voice_queued', 'image_saved', "
            "'projects_listed', 'project_name_mode_set', "
            "'project_name_required', 'project_created', "
            "'project_selected', 'project_cleared', "
            "'memory_mode_set', 'memory_mode_cancelled', "
            "'memory_question_queued', 'memory_question_required', "
            "'language_prompt_shown', 'language_selected', "
            "'invite_created', 'invite_forbidden', 'already_enrolled', "
            "'api_tokens_listed', 'api_token_created', 'api_token_revoked', "
            "'contact_saved', 'digest_menu_shown', 'digest_shown', 'ignored')",
            name=RESULT_KIND_CHECK_NAME,
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_telegram_update_receipts_trace_id",
        ),
    )

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    result_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TelegramUpdateInbox(Base):
    """Webhook-INBOX: сырой Telegram-апдейт до обработки воркером.

    Технический путь ДО резолва пользователя (как telegram_update_receipts):
    без user_space_id и без RLS — пользователя позже резолвит процессор.
    payload — сырой апдейт целиком (PII: default-repr SQLAlchemy колонок не
    показывает, в логи payload не попадает). Уникальность (bot_id, update_id)
    гасит ретраи Telegram; порядок обработки — строго по update_id.
    """

    __tablename__ = "telegram_update_inbox"
    __table_args__ = (
        UniqueConstraint(
            "bot_id", "update_id", name="uq_telegram_update_inbox_delivery"
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_telegram_update_inbox_attempt_count"
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="ck_telegram_update_inbox_trace_id",
        ),
        # Скан головы: pending-строки бота в порядке update_id.
        Index(
            "ix_telegram_update_inbox_bot_status_update",
            "bot_id",
            "status",
            "update_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[TelegramInboxStatus] = mapped_column(
        Enum(
            TelegramInboxStatus,
            name="telegram_inbox_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)


class EnrollmentAttempt(Base):
    __tablename__ = "enrollment_attempts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    pepper_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_code: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

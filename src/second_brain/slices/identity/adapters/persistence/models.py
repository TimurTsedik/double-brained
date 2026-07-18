from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.persistence.base import Base

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

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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role = 'admin'", name="ck_users_role_admin"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class UserSpace(Base):
    __tablename__ = "user_spaces"
    __table_args__ = (
        CheckConstraint(
            "timezone = 'Asia/Jerusalem'",
            name="ck_user_spaces_timezone_asia_jerusalem",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
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


class EnrollmentInvite(Base):
    __tablename__ = "enrollment_invites"
    __table_args__ = (
        CheckConstraint("role = 'admin'", name="ck_enrollment_invites_role_admin"),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'expired', 'revoked')",
            name="ck_enrollment_invites_status",
        ),
        CheckConstraint(
            "created_by_actor = 'bootstrap_cli'",
            name="ck_enrollment_invites_bootstrap_actor",
        ),
        Index(
            "uq_enrollment_invites_pending_bootstrap",
            "status",
            unique=True,
            postgresql_where=text("status = 'pending'"),
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


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipts"
    __table_args__ = (
        CheckConstraint(
            "result_kind IN "
            "('enrolled', 'enrollment_rejected', 'known_user_started', 'ignored')",
            name="ck_telegram_update_receipts_result_kind",
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

from datetime import datetime, timedelta
from hashlib import blake2b
from hmac import compare_digest
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from second_brain.shared.i18n import Locale, resolve_locale
from second_brain.slices.identity.adapters.persistence.models import (
    ApiToken,
    EnrollmentAttempt,
    EnrollmentInvite,
    TelegramIdentity,
    TelegramUpdateReceipt,
    User,
    UserSpace,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    PanelContext,
    TelegramRecipient,
)
from second_brain.slices.identity.ports.repositories import (
    ApiTokenPrincipal,
    ApiTokenView,
    BootstrapInviteUnavailable,
    EnrollmentAttemptReservation,
    EnrollmentOutcome,
    NewApiToken,
    NewBootstrapInvite,
    StoredUpdateReceipt,
    UpdateHandler,
)

BOOTSTRAP_LOCK_KEY = 487_251_309
MAX_ENROLLMENT_ATTEMPTS = 5
UPDATE_LOCK_NAMESPACE = b"identity-update-lock-v1"
POLLER_LOCK_KEY_NAMESPACE = b"identity-poller-lock-v1"


class PostgresEnrollmentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def store_bootstrap_invite(self, invite: NewBootstrapInvite) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_bootstrap_lock(session)
                active_user = await session.scalar(
                    select(User.id).where(User.is_active.is_(True)).limit(1)
                )
                pending_invite = await session.scalar(
                    select(EnrollmentInvite.id)
                    .where(EnrollmentInvite.status == "pending")
                    .limit(1)
                )
                if active_user is not None or pending_invite is not None:
                    raise BootstrapInviteUnavailable("bootstrap invite is unavailable")
                session.add(
                    EnrollmentInvite(
                        id=invite.id,
                        token_hash=invite.token_hash,
                        pepper_key_id=invite.pepper_key_id,
                        role=invite.role,
                        status="pending",
                        created_by_actor="bootstrap_cli",
                        created_at=invite.created_at,
                        expires_at=invite.expires_at,
                    )
                )

    async def enroll_telegram_user(
        self,
        token_hash: bytes,
        pepper_key_id: str,
        telegram_user_id: int,
        now: datetime,
    ) -> EnrollmentOutcome:
        async with self._session_factory() as session:
            async with session.begin():
                return await enroll_telegram_user_in_session(
                    session, token_hash, pepper_key_id, telegram_user_id, now
                )


async def enroll_telegram_user_in_session(
    session: AsyncSession,
    token_hash: bytes,
    pepper_key_id: str,
    telegram_user_id: int,
    now: datetime,
) -> EnrollmentOutcome:
    await acquire_bootstrap_lock(session)
    # 1. Дедуп телеграма ДО потребления invite и вставки User (M3): у аккаунта уже
    #    есть активный вход → «уже подключён», ничего не жжём и не вставляем.
    active_identity = await session.scalar(
        select(TelegramIdentity.id)
        .where(
            TelegramIdentity.telegram_user_id == telegram_user_id,
            TelegramIdentity.is_active.is_(True),
        )
        .limit(1)
    )
    if active_identity is not None:
        return EnrollmentOutcome.ALREADY_ENROLLED

    # 2. Выбор invite по (token_hash И текущий pepper_key_id) под row-lock (M2):
    #    token_hash UNIQUE, поэтому это ровно нужная строка; compare_digest —
    #    постоянное время. Устаревший pepper_key_id при совпавшем хэше не пройдёт.
    invite = await session.scalar(
        select(EnrollmentInvite)
        .where(
            EnrollmentInvite.token_hash == token_hash,
            EnrollmentInvite.pepper_key_id == pepper_key_id,
            EnrollmentInvite.status == "pending",
        )
        .with_for_update()
    )
    if invite is None or not compare_digest(invite.token_hash, token_hash):
        return EnrollmentOutcome.REJECTED
    if invite.expires_at <= now:
        invite.status = "expired"
        return EnrollmentOutcome.REJECTED

    # 3. admin-invite потребляется ровно один раз: если активный admin уже есть —
    #    REJECTED, invite НЕ трогаем. Для member-invite гейта нет.
    if invite.role == "admin":
        active_admin = await session.scalar(
            select(User.id)
            .where(User.role == "admin", User.is_active.is_(True))
            .limit(1)
        )
        if active_admin is not None:
            return EnrollmentOutcome.REJECTED

    user = User(id=uuid4(), role=invite.role, created_at=now, updated_at=now)
    session.add(user)
    await session.flush()
    session.add_all(
        [
            UserSpace(
                id=uuid4(),
                owner_user_id=user.id,
                timezone="Asia/Jerusalem",
                created_at=now,
                updated_at=now,
            ),
            TelegramIdentity(
                id=uuid4(),
                telegram_user_id=telegram_user_id,
                user_id=user.id,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    invite.status = "consumed"
    invite.consumed_at = now
    invite.consumed_user_id = user.id
    await session.flush()
    return EnrollmentOutcome.ENROLLED


async def acquire_bootstrap_lock(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": BOOTSTRAP_LOCK_KEY},
    )


class PostgresUpdateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def process_once(
        self,
        bot_id: int,
        update_id: int,
        occurred_at: datetime,
        handler: UpdateHandler,
    ) -> StoredUpdateReceipt:
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_update_lock(session, bot_id, update_id)
                receipt = await _load_receipt(session, bot_id, update_id)
                if receipt is not None:
                    return StoredUpdateReceipt(
                        receipt.result_kind, receipt.trace_id, existing=True
                    )

                result = await handler(PostgresUpdateTransaction(session))
                inserted_trace_id = await session.scalar(
                    insert(TelegramUpdateReceipt)
                    .values(
                        bot_id=bot_id,
                        update_id=update_id,
                        result_kind=result.result_kind,
                        trace_id=result.trace_id,
                        created_at=occurred_at,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            TelegramUpdateReceipt.bot_id,
                            TelegramUpdateReceipt.update_id,
                        ]
                    )
                    .returning(TelegramUpdateReceipt.trace_id)
                )
                if inserted_trace_id is not None:
                    return StoredUpdateReceipt(
                        result.result_kind,
                        result.trace_id,
                        existing=False,
                        span_id=result.span_id,
                    )

                receipt = await _load_receipt(session, bot_id, update_id)
                if receipt is None:
                    raise RuntimeError("receipt conflict could not be reloaded")
                return StoredUpdateReceipt(
                    receipt.result_kind, receipt.trace_id, existing=True
                )


class PostgresAccessContextResolver:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_access_context(
        self, telegram_user_id: int
    ) -> AccessContext | None:
        async with self._session_factory() as session:
            return await resolve_access_context_in_session(session, telegram_user_id)


class PostgresWorkerIdentityRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_access_contexts(self) -> tuple[AccessContext, ...]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(User.id, UserSpace.id)
                .select_from(TelegramIdentity)
                .join(User, User.id == TelegramIdentity.user_id)
                .join(UserSpace, UserSpace.owner_user_id == User.id)
                .where(
                    TelegramIdentity.is_active.is_(True),
                    User.is_active.is_(True),
                    UserSpace.is_active.is_(True),
                )
                .order_by(UserSpace.id)
            )
            return tuple(
                AccessContext(user_id=row[0], user_space_id=row[1]) for row in rows
            )

    async def resolve_telegram_recipient(
        self, access_context: AccessContext
    ) -> TelegramRecipient:
        async with self._session_factory() as session:
            telegram_user_id = await session.scalar(
                select(TelegramIdentity.telegram_user_id)
                .select_from(TelegramIdentity)
                .join(User, User.id == TelegramIdentity.user_id)
                .join(UserSpace, UserSpace.owner_user_id == User.id)
                .where(
                    TelegramIdentity.user_id == access_context.user_id,
                    User.id == access_context.user_id,
                    UserSpace.id == access_context.user_space_id,
                    UserSpace.owner_user_id == access_context.user_id,
                    TelegramIdentity.is_active.is_(True),
                    User.is_active.is_(True),
                    UserSpace.is_active.is_(True),
                )
                .limit(1)
            )
        if telegram_user_id is None:
            raise LookupError("active Telegram recipient was not found")
        return TelegramRecipient(telegram_user_id=telegram_user_id)

    async def resolve_locale(self, access_context: AccessContext) -> Locale:
        async with self._session_factory() as session:
            language = await read_user_space_language(
                session, access_context.user_space_id, access_context.user_id
            )
        return resolve_locale(language)


async def read_user_space_language(
    session: AsyncSession, user_space_id: UUID, owner_user_id: UUID
) -> str | None:
    # Owner-предикат симметрично set_user_space_language: язык читается только из
    # СВОЕГО space, ошибочно сшитый AccessContext не достанет чужой.
    return await session.scalar(
        select(UserSpace.language).where(
            UserSpace.id == user_space_id,
            UserSpace.owner_user_id == owner_user_id,
        )
    )


async def read_user_space_timezone(
    session: AsyncSession, user_space_id: UUID, owner_user_id: UUID
) -> str | None:
    # Owner-предикат как у read_user_space_language: часовой пояс читается только
    # из СВОЕГО space, ошибочно сшитый AccessContext не достанет чужой.
    return cast(
        str | None,
        await session.scalar(
            select(UserSpace.timezone).where(
                UserSpace.id == user_space_id,
                UserSpace.owner_user_id == owner_user_id,
            )
        ),
    )


async def set_user_space_language(
    session: AsyncSession,
    user_space_id: UUID,
    owner_user_id: UUID,
    language: str,
    updated_at: datetime,
) -> bool:
    # Owner-предикат: право менять язык только в СВОЁМ space (на user_spaces RLS
    # нет — изоляция через owner_user_id в WHERE). updated_at бампается, поэтому
    # колоночный грант покрывает оба столбца.
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(UserSpace)
            .where(
                UserSpace.id == user_space_id,
                UserSpace.owner_user_id == owner_user_id,
            )
            .values(language=language, updated_at=updated_at)
        ),
    )
    return result.rowcount == 1


async def read_language_by_telegram_user(
    session: AsyncSession, telegram_user_id: int
) -> str | None:
    return await session.scalar(
        select(UserSpace.language)
        .select_from(TelegramIdentity)
        .join(User, User.id == TelegramIdentity.user_id)
        .join(UserSpace, UserSpace.owner_user_id == User.id)
        .where(
            TelegramIdentity.telegram_user_id == telegram_user_id,
            TelegramIdentity.is_active.is_(True),
            User.is_active.is_(True),
            UserSpace.is_active.is_(True),
        )
        .limit(1)
    )


class PostgresLocaleResolver:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_for_telegram_user(self, telegram_user_id: int) -> Locale:
        async with self._session_factory() as session:
            language = await read_language_by_telegram_user(session, telegram_user_id)
        return resolve_locale(language)


async def read_panel_context_by_telegram_user(
    session: AsyncSession, telegram_user_id: int
) -> tuple[str | None, str] | None:
    # Один round-trip для панели: язык (для locale) и роль (для is_admin) лежат на
    # ОДНОМ join-пути TelegramIdentity→User→UserSpace. Те же active/owner-предикаты,
    # что и у раздельных резолверов locale/admin, — поведение идентично.
    row = (
        await session.execute(
            select(UserSpace.language, User.role)
            .select_from(TelegramIdentity)
            .join(User, User.id == TelegramIdentity.user_id)
            .join(UserSpace, UserSpace.owner_user_id == User.id)
            .where(
                TelegramIdentity.telegram_user_id == telegram_user_id,
                TelegramIdentity.is_active.is_(True),
                User.is_active.is_(True),
                UserSpace.is_active.is_(True),
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    return (row[0], row[1])


class PostgresPanelContextResolver:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_panel_context(self, telegram_user_id: int) -> PanelContext:
        async with self._session_factory() as session:
            result = await read_panel_context_by_telegram_user(
                session, telegram_user_id
            )
        if result is None:
            # Неизвестный/неактивный: тот же fallback, что и раньше (locale по
            # None, is_admin=False).
            return PanelContext(locale=resolve_locale(None), is_admin=False)
        language, role = result
        return PanelContext(locale=resolve_locale(language), is_admin=role == "admin")


async def resolve_access_context_in_session(
    session: AsyncSession, telegram_user_id: int
) -> AccessContext | None:
    row = (
        await session.execute(
            select(User.id, UserSpace.id)
            .select_from(TelegramIdentity)
            .join(User, User.id == TelegramIdentity.user_id)
            .join(UserSpace, UserSpace.owner_user_id == User.id)
            .where(
                TelegramIdentity.telegram_user_id == telegram_user_id,
                TelegramIdentity.is_active.is_(True),
                User.is_active.is_(True),
                UserSpace.is_active.is_(True),
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    return AccessContext(user_id=row[0], user_space_id=row[1])


class PostgresUpdateTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def active_session(self) -> AsyncSession:
        """Restricted to bootstrap transaction adapters."""
        return self._session

    async def resolve_access_context(
        self, telegram_user_id: int
    ) -> AccessContext | None:
        return await resolve_access_context_in_session(self._session, telegram_user_id)

    async def reserve_enrollment_attempt(
        self,
        bot_id: int,
        actor_digest: bytes,
        pepper_key_id: str,
        trace_id: str,
        created_at: datetime,
    ) -> EnrollmentAttemptReservation:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": int.from_bytes(actor_digest[:8], signed=True)},
        )
        attempt_count = await self._session.scalar(
            select(func.count())
            .select_from(EnrollmentAttempt)
            .where(
                EnrollmentAttempt.bot_id == bot_id,
                EnrollmentAttempt.actor_digest == actor_digest,
                EnrollmentAttempt.created_at >= created_at - timedelta(minutes=15),
            )
        )
        admitted = int(attempt_count or 0) < MAX_ENROLLMENT_ATTEMPTS
        attempt = EnrollmentAttempt(
            id=uuid4(),
            bot_id=bot_id,
            actor_digest=actor_digest,
            pepper_key_id=pepper_key_id,
            result_code="pending" if admitted else "rate_limited",
            trace_id=trace_id,
            created_at=created_at,
        )
        self._session.add(attempt)
        await self._session.flush()
        return EnrollmentAttemptReservation(attempt.id, admitted)

    async def finish_enrollment_attempt(
        self, attempt_id: UUID, result_code: str
    ) -> None:
        attempt = await self._session.get(EnrollmentAttempt, attempt_id)
        if attempt is None:
            raise RuntimeError("enrollment attempt was not reserved")
        attempt.result_code = result_code

    async def enroll_telegram_user(
        self,
        token_hash: bytes,
        pepper_key_id: str,
        telegram_user_id: int,
        now: datetime,
    ) -> EnrollmentOutcome:
        return await enroll_telegram_user_in_session(
            self._session, token_hash, pepper_key_id, telegram_user_id, now
        )

    async def create_member_invite(
        self,
        telegram_user_id: int,
        token_hash: bytes,
        pepper_key_id: str,
        now: datetime,
    ) -> bool:
        # Скрытие кнопки ≠ авторизация (M7): заново резолвим инициатора как
        # АКТИВНОГО admin по telegram_user_id ЭТОГО callback'а. Member/inactive/
        # unknown → invite не создаётся (False, ссылку не шлём).
        admin_user_id = await self._session.scalar(
            select(User.id)
            .select_from(TelegramIdentity)
            .join(User, User.id == TelegramIdentity.user_id)
            .where(
                TelegramIdentity.telegram_user_id == telegram_user_id,
                TelegramIdentity.is_active.is_(True),
                User.is_active.is_(True),
                User.role == "admin",
            )
            .limit(1)
        )
        if admin_user_id is None:
            return False
        self._session.add(
            EnrollmentInvite(
                id=uuid4(),
                token_hash=token_hash,
                pepper_key_id=pepper_key_id,
                role="member",
                status="pending",
                created_by_actor="admin_bot",
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
        await self._session.flush()
        return True

    async def read_user_space_language(
        self, access_context: AccessContext
    ) -> str | None:
        return await read_user_space_language(
            self._session, access_context.user_space_id, access_context.user_id
        )

    async def set_user_space_language(
        self, access_context: AccessContext, language: str, now: datetime
    ) -> bool:
        return await set_user_space_language(
            self._session,
            access_context.user_space_id,
            access_context.user_id,
            language,
            now,
        )

    async def read_user_space_timezone(
        self, access_context: AccessContext
    ) -> str | None:
        return await read_user_space_timezone(
            self._session, access_context.user_space_id, access_context.user_id
        )

    async def issue_api_token(
        self, access_context: AccessContext, token: NewApiToken
    ) -> ApiTokenView:
        # На api_tokens RLS нет (проверка токена идёт до резолва пространства),
        # поэтому изоляция здесь — user_id из access_context: чужой владелец в
        # строку не запишется.
        row = ApiToken(
            id=token.id,
            user_id=access_context.user_id,
            token_hash=token.token_hash,
            pepper_key_id=token.pepper_key_id,
            label=token.label,
            created_at=token.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _api_token_view(row)

    async def list_api_tokens(
        self, access_context: AccessContext
    ) -> tuple[ApiTokenView, ...]:
        rows = await self._session.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == access_context.user_id)
            .order_by(ApiToken.created_at, ApiToken.id)
        )
        return tuple(_api_token_view(row) for row in rows)

    async def revoke_api_token(
        self, access_context: AccessContext, token_id: UUID, now: datetime
    ) -> bool:
        # Отзыв — пометка, а не удаление. Повторный отзыв не переписывает момент
        # (revoked_at IS NULL в WHERE), но и не считается ошибкой: наличие строки
        # проверяется отдельно.
        token = await self._session.scalar(
            select(ApiToken).where(
                ApiToken.id == token_id,
                ApiToken.user_id == access_context.user_id,
            )
        )
        if token is None:
            return False
        if token.revoked_at is None:
            token.revoked_at = now
            await self._session.flush()
        return True


def _api_token_view(row: ApiToken) -> ApiTokenView:
    return ApiTokenView(
        id=row.id,
        label=row.label,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


class PostgresApiTokenRepository:
    """Проверка предъявленного секрета вне транзакции обработки апдейта.

    Живёт отдельно от бот-пути: HTTP-запрос приходит без пространства, и именно
    этот запрос его и устанавливает. Отметка last_used_at пишется только когда
    она реально устарела — обычный запрос API остаётся чистым чтением, — и
    только вперёд: параллельные запросы одним токеном не двигают её назад.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def authenticate_api_token(
        self,
        token_hash: bytes,
        pepper_key_id: str,
        now: datetime,
        refresh_used_before: datetime,
    ) -> ApiTokenPrincipal | None:
        async with self._session_factory() as session:
            async with session.begin():
                # Пара (хэш, текущий pepper_key_id) — как у приглашений: токены,
                # выданные под прежним перцем, перестают проходить сами собой.
                row = (
                    await session.execute(
                        select(
                            ApiToken.id,
                            ApiToken.token_hash,
                            ApiToken.last_used_at,
                            User.id,
                            UserSpace.id,
                        )
                        .select_from(ApiToken)
                        .join(User, User.id == ApiToken.user_id)
                        .join(UserSpace, UserSpace.owner_user_id == User.id)
                        .where(
                            ApiToken.token_hash == token_hash,
                            ApiToken.pepper_key_id == pepper_key_id,
                            ApiToken.revoked_at.is_(None),
                            User.is_active.is_(True),
                            UserSpace.is_active.is_(True),
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if row is None:
                    return None
                token_id, stored_hash, last_used_at, user_id, user_space_id = row
                if not compare_digest(stored_hash, token_hash):
                    return None
                if last_used_at is None or last_used_at < refresh_used_before:
                    # Прочитанная строка решает только «пора ли писать» — так
                    # обычный запрос не превращается в запись. Условие самой
                    # записи проверяет БАЗА и заново: между чтением и UPDATE
                    # строку успевает изменить соседняя транзакция. Отсюда два
                    # условия в WHERE — токен ещё жив (отозванному отметка не
                    # ставится) и новый момент строго позже прежнего. При READ
                    # COMMITTED UPDATE перепроверяет WHERE на свежей версии
                    # строки, поэтому более ранняя отметка не ляжет поверх более
                    # поздней ни при каком порядке коммитов.
                    await session.execute(
                        update(ApiToken)
                        .where(
                            ApiToken.id == token_id,
                            ApiToken.user_id == user_id,
                            ApiToken.revoked_at.is_(None),
                            or_(
                                ApiToken.last_used_at.is_(None),
                                ApiToken.last_used_at < now,
                            ),
                        )
                        .values(last_used_at=now)
                    )
                return ApiTokenPrincipal(
                    access_context=AccessContext(
                        user_id=user_id, user_space_id=user_space_id
                    ),
                    token_id=token_id,
                )


async def acquire_update_lock(
    session: AsyncSession, bot_id: int, update_id: int
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": advisory_key(UPDATE_LOCK_NAMESPACE, bot_id, update_id)},
    )


def advisory_key(namespace: bytes, *identifiers: int) -> int:
    encoded_identifiers = b":".join(
        str(identifier).encode() for identifier in identifiers
    )
    digest = blake2b(namespace + b":" + encoded_identifiers, digest_size=8).digest()
    return int.from_bytes(digest, signed=True)


async def _load_receipt(
    session: AsyncSession, bot_id: int, update_id: int
) -> TelegramUpdateReceipt | None:
    return cast(
        TelegramUpdateReceipt | None,
        await session.scalar(
            select(TelegramUpdateReceipt).where(
                TelegramUpdateReceipt.bot_id == bot_id,
                TelegramUpdateReceipt.update_id == update_id,
            )
        ),
    )


class PostgresPollerLock:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None

    async def acquire(self, bot_id: int) -> bool:
        if self._connection is not None:
            return True
        connection = await self._engine.connect()
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": advisory_key(POLLER_LOCK_KEY_NAMESPACE, bot_id)},
        )
        if not acquired:
            await connection.close()
            return False
        self._connection = connection
        return True

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

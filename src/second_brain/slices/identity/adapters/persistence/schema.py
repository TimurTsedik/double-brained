from typing import cast

from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.persistence.base import Base
from second_brain.slices.identity.adapters.persistence.models import (
    ACTIVE_ADMIN_INDEX_NAME,
    ENROLLMENT_INVITE_ACTOR_CHECK_NAME,
    ENROLLMENT_INVITE_PENDING_INDEX_NAME,
    ENROLLMENT_INVITE_ROLE_CHECK_NAME,
    RESULT_KIND_CHECK_NAME,
    USER_ROLE_CHECK_NAME,
    USER_SPACE_LANGUAGE_CHECK_NAME,
    ApiToken,
    EnrollmentAttempt,
    EnrollmentInvite,
    TelegramIdentity,
    TelegramUpdateInbox,
    TelegramUpdateReceipt,
    User,
    UserSpace,
)

APPLICATION_ROLE = "second_brain_app"
IDENTITY_TABLES = (
    cast(Table, User.__table__),
    cast(Table, UserSpace.__table__),
    cast(Table, TelegramIdentity.__table__),
    cast(Table, EnrollmentInvite.__table__),
    cast(Table, TelegramUpdateReceipt.__table__),
    cast(Table, TelegramUpdateInbox.__table__),
    cast(Table, EnrollmentAttempt.__table__),
    # api_tokens — БЕЗ RLS (причина в докстринге модели). Отдельной реконсиляции
    # не требует: create_all(checkfirst=True) пропускает лишь СУЩЕСТВУЮЩИЕ
    # таблицы, а недостающую доращивает — живая база секции B получит её первым
    # же init-db.
    cast(Table, ApiToken.__table__),
)


async def initialize_identity_schema(
    engine: AsyncEngine, schema_name: str = "public"
) -> None:
    async with engine.begin() as connection:
        await _ensure_application_role(connection)
        await connection.run_sync(_create_identity_tables)
        await _reconcile_result_kind_check(connection, schema_name)
        await _reconcile_user_space_language(connection, schema_name)
        await _reconcile_multi_user_constraints(connection, schema_name)
        await _grant_application_privileges(connection, schema_name)


async def reset_identity_prototype_schema(
    engine: AsyncEngine, confirm: bool, schema_name: str = "public"
) -> None:
    if not confirm:
        raise ValueError("prototype schema reset requires confirmation")

    async with engine.begin() as connection:
        await _ensure_application_role(connection)
        await connection.run_sync(_drop_identity_tables)
        await connection.run_sync(_create_identity_tables)
        await _reconcile_user_space_language(connection, schema_name)
        await _grant_application_privileges(connection, schema_name)


async def _ensure_application_role(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            "DO $$ "
            "BEGIN "
            "CREATE ROLE second_brain_app LOGIN NOSUPERUSER NOBYPASSRLS; "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$"
        )
    )
    await connection.execute(
        text(
            "ALTER ROLE second_brain_app LOGIN NOSUPERUSER NOBYPASSRLS "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT"
        )
    )


def _create_identity_tables(connection: Connection) -> None:
    Base.metadata.create_all(connection, tables=IDENTITY_TABLES)


async def _reconcile_result_kind_check(
    connection: AsyncConnection, schema_name: str
) -> None:
    # create_all(checkfirst=True) skips an existing table, so a live prototype DB
    # (slices 1-2) keeps its OLD result_kind CHECK and would reject the memory_*
    # kinds. Re-apply the current ORM definition idempotently: harmless drop+add
    # of the same predicate on a fresh DB, a repair on an existing one. Existing
    # rows never violate the new set (it is a strict superset), so ADD is safe.
    expression = _result_kind_check_expression()
    table = f"{_quote_identifier(schema_name)}.telegram_update_receipts"
    quoted_name = _quote_identifier(RESULT_KIND_CHECK_NAME)
    await connection.execute(
        text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {quoted_name}")
    )
    await connection.execute(
        text(f"ALTER TABLE {table} ADD CONSTRAINT {quoted_name} CHECK ({expression})")
    )


def _result_kind_check_expression() -> str:
    table = cast(Table, TelegramUpdateReceipt.__table__)
    for constraint in table.constraints:
        if (
            isinstance(constraint, CheckConstraint)
            and constraint.name == RESULT_KIND_CHECK_NAME
        ):
            return str(constraint.sqltext)
    raise RuntimeError("result_kind CHECK constraint is missing from the ORM model")


async def _reconcile_multi_user_constraints(
    connection: AsyncConnection, schema_name: str
) -> None:
    # Живая прод-база слайсов 1–3 несёт ОДНОпользовательские замки: role='admin'
    # CHECK'и, created_by_actor='bootstrap_cli' CHECK и частичный уникальный индекс
    # «один pending». create_all(checkfirst=True) их не трогает. Снимаем и
    # переприменяем текущие ORM-определения идемпотентно (drop+add того же имени —
    # на свежей БД повтор того же предиката, на живой — ремонт). Ослабление
    # CHECK'ов forward-only: новый набор — строгий супермножество старого, ни одна
    # существующая строка его не нарушает.
    quoted_schema = _quote_identifier(schema_name)
    users = f"{quoted_schema}.users"
    invites = f"{quoted_schema}.enrollment_invites"
    await _reapply_check(connection, users, USER_ROLE_CHECK_NAME, User)
    await _reapply_check(
        connection, invites, ENROLLMENT_INVITE_ROLE_CHECK_NAME, EnrollmentInvite
    )
    await _reapply_check(
        connection, invites, ENROLLMENT_INVITE_ACTOR_CHECK_NAME, EnrollmentInvite
    )
    # Снять легаси «один pending» — разрешить несколько висящих приглашений.
    await connection.execute(
        text(
            "DROP INDEX IF EXISTS "
            f"{quoted_schema}.{_quote_identifier(ENROLLMENT_INVITE_PENDING_INDEX_NAME)}"
        )
    )
    # Инвариант «один активный admin» на уровень БД (M9), идемпотентно.
    await connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            f"{_quote_identifier(ACTIVE_ADMIN_INDEX_NAME)} ON {users} (role) "
            "WHERE role = 'admin' AND is_active"
        )
    )


async def _reapply_check(
    connection: AsyncConnection, table: str, check_name: str, model: type[object]
) -> None:
    expression = _check_expression(model, check_name)
    quoted_name = _quote_identifier(check_name)
    await connection.execute(
        text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {quoted_name}")
    )
    await connection.execute(
        text(f"ALTER TABLE {table} ADD CONSTRAINT {quoted_name} CHECK ({expression})")
    )


def _check_expression(model: type[object], check_name: str) -> str:
    table = cast(Table, model.__table__)  # type: ignore[attr-defined]
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == check_name:
            return str(constraint.sqltext)
    raise RuntimeError(f"CHECK constraint {check_name} is missing from the ORM model")


async def _reconcile_user_space_language(
    connection: AsyncConnection, schema_name: str
) -> None:
    # create_all(checkfirst=True) skips an existing user_spaces table, so a live
    # prototype DB (slices 1-3) never gains the new language column. Add it
    # idempotently (ADD COLUMN IF NOT EXISTS) and re-apply its CHECK: on a fresh
    # DB the column already exists (no-op) and the drop+add repeats the same
    # predicate; on a live DB both statements materialize it. Existing rows get
    # NULL (language not chosen yet → effective RU), so the ADD is forward-only.
    table = f"{_quote_identifier(schema_name)}.user_spaces"
    await connection.execute(
        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS language VARCHAR(2)")
    )
    expression = _user_space_language_check_expression()
    quoted_name = _quote_identifier(USER_SPACE_LANGUAGE_CHECK_NAME)
    await connection.execute(
        text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {quoted_name}")
    )
    await connection.execute(
        text(f"ALTER TABLE {table} ADD CONSTRAINT {quoted_name} CHECK ({expression})")
    )


def _user_space_language_check_expression() -> str:
    table = cast(Table, UserSpace.__table__)
    for constraint in table.constraints:
        if (
            isinstance(constraint, CheckConstraint)
            and constraint.name == USER_SPACE_LANGUAGE_CHECK_NAME
        ):
            return str(constraint.sqltext)
    raise RuntimeError("language CHECK constraint is missing from the ORM model")


def _drop_identity_tables(connection: Connection) -> None:
    Base.metadata.drop_all(connection, tables=IDENTITY_TABLES)


async def _grant_application_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    database_name = await connection.scalar(text("SELECT current_database()"))
    if not isinstance(database_name, str):
        raise RuntimeError("PostgreSQL did not return a database name")

    quoted_database = _quote_identifier(database_name)
    quoted_schema = _quote_identifier(schema_name)
    enrollment_attempts = f"{quoted_schema}.enrollment_attempts"
    enrollment_invites = f"{quoted_schema}.enrollment_invites"
    user_spaces = f"{quoted_schema}.user_spaces"
    update_inbox = f"{quoted_schema}.telegram_update_inbox"
    api_tokens = f"{quoted_schema}.api_tokens"
    await connection.execute(
        text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
            f"{quoted_schema} FROM {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA "
            f"{quoted_schema} TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "GRANT UPDATE ON TABLE "
            f"{enrollment_attempts}, {enrollment_invites} "
            f"TO {APPLICATION_ROLE}"
        )
    )
    # Webhook-INBOX: enqueue=INSERT (роут), claim/итог шага=КОЛОНОЧНЫЙ UPDATE
    # только по прогрессу обработки — сам апдейт (payload/bot_id/update_id/
    # trace_id/received_at) app-роль переписать не может. Без DELETE. Широкий
    # табличный грант живой базы снимает REVOKE ALL выше: он забирает и
    # колоночные права, поэтому старое право не остаётся висеть.
    await connection.execute(
        text(
            "GRANT UPDATE (status, attempt_count, next_attempt_at) ON TABLE "
            f"{update_inbox} TO {APPLICATION_ROLE}"
        )
    )
    # Токены API: выдача=INSERT, чтение/проверка=SELECT (оба из широкого гранта),
    # КОЛОНОЧНЫЙ UPDATE только по двум отметкам жизненного цикла. Ни секрет
    # (token_hash), ни владельца (user_id), ни перец app-роль переписать не
    # может. DELETE не даётся: отзыв — это revoked_at, история остаётся.
    await connection.execute(
        text(
            "GRANT UPDATE (last_used_at, revoked_at) ON TABLE "
            f"{api_tokens} TO {APPLICATION_ROLE}"
        )
    )
    # КОЛОНОЧНЫЙ грант (решение 3): app-роль меняет только язык (и updated_at,
    # который бампается при смене) — но НЕ owner_user_id/timezone/is_active.
    await connection.execute(
        text(
            "GRANT UPDATE (language, updated_at) ON TABLE "
            f"{user_spaces} TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA "
            f"{quoted_schema} REVOKE ALL ON TABLES FROM {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA "
            f"{quoted_schema} GRANT SELECT, INSERT ON TABLES TO {APPLICATION_ROLE}"
        )
    )


def _quote_identifier(value: str) -> str:
    return f'"{value.replace('"', '""')}"'

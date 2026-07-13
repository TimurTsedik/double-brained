from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.slices.capture.adapters.persistence.models import CaptureEventModel
from second_brain.slices.identity.adapters.persistence.schema import (
    APPLICATION_ROLE,
    initialize_identity_schema,
    reset_identity_prototype_schema,
)
from second_brain.slices.knowledge.adapters.persistence.models import (
    DecisionModel,
    DecisionProvenanceModel,
    IdeaModel,
    IdeaProvenanceModel,
    NoteModel,
    NoteProvenanceModel,
    QuestionModel,
    QuestionProvenanceModel,
)
from second_brain.slices.retrieval.adapters.persistence.models import (
    PendingSearchModeModel,
)
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
    TaskProvenanceModel,
)

CAPTURE_EVENT_TABLE = cast(Table, CaptureEventModel.__table__)
TASK_TABLES = (
    cast(Table, TaskModel.__table__),
    cast(Table, TaskProvenanceModel.__table__),
    cast(Table, PendingCaptureSelectionModel.__table__),
)
KNOWLEDGE_TABLES = (
    cast(Table, NoteModel.__table__),
    cast(Table, NoteProvenanceModel.__table__),
    cast(Table, IdeaModel.__table__),
    cast(Table, IdeaProvenanceModel.__table__),
    cast(Table, DecisionModel.__table__),
    cast(Table, DecisionProvenanceModel.__table__),
    cast(Table, QuestionModel.__table__),
    cast(Table, QuestionProvenanceModel.__table__),
)
PENDING_SEARCH_MODE_TABLE = cast(Table, PendingSearchModeModel.__table__)


async def initialize_schema(engine: AsyncEngine, schema_name: str = "public") -> None:
    await initialize_identity_schema(engine, schema_name)
    await _initialize_capture_schema(engine, schema_name)
    await _initialize_task_schema(engine, schema_name)
    await _initialize_knowledge_schema(engine, schema_name)
    await _initialize_retrieval_schema(engine, schema_name)


async def reset_prototype_schema(
    engine: AsyncEngine, confirm: bool, schema_name: str = "public"
) -> None:
    if not confirm:
        await reset_identity_prototype_schema(engine, confirm, schema_name)
        return
    await _drop_retrieval_schema(engine)
    await _drop_task_schema(engine)
    await _drop_knowledge_schema(engine)
    await _drop_capture_schema(engine)
    await reset_identity_prototype_schema(engine, confirm, schema_name)
    await _initialize_capture_schema(engine, schema_name)
    await _initialize_task_schema(engine, schema_name)
    await _initialize_knowledge_schema(engine, schema_name)
    await _initialize_retrieval_schema(engine, schema_name)


async def _initialize_capture_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_capture_event_table)
        await _configure_capture_event_rls(connection, schema_name)


async def _drop_capture_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_capture_event_table)


async def _initialize_task_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_task_tables)
        for table_name in (
            "tasks",
            "task_provenance",
            "pending_capture_selections",
        ):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_task_privileges(connection, schema_name)


async def _drop_task_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_task_tables)


async def _initialize_knowledge_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_knowledge_tables)
        for table_name in (
            "notes",
            "note_provenance",
            "ideas",
            "idea_provenance",
            "decisions",
            "decision_provenance",
            "questions",
            "question_provenance",
        ):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_knowledge_privileges(connection, schema_name)


async def _drop_knowledge_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_knowledge_tables)


async def _initialize_retrieval_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_pending_search_mode_table)
        await _configure_user_space_rls(connection, schema_name, "pending_search_modes")
        await _grant_retrieval_privileges(connection, schema_name)
        await _create_full_text_indexes(connection, schema_name)


async def _drop_retrieval_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_pending_search_mode_table)


def _create_capture_event_table(connection: Connection) -> None:
    CAPTURE_EVENT_TABLE.create(connection, checkfirst=True)


def _drop_capture_event_table(connection: Connection) -> None:
    CAPTURE_EVENT_TABLE.drop(connection, checkfirst=True)


def _create_task_tables(connection: Connection) -> None:
    for table in TASK_TABLES:
        table.create(connection, checkfirst=True)


def _drop_task_tables(connection: Connection) -> None:
    for table in reversed(TASK_TABLES):
        table.drop(connection, checkfirst=True)


def _create_knowledge_tables(connection: Connection) -> None:
    for table in KNOWLEDGE_TABLES:
        table.create(connection, checkfirst=True)


def _drop_knowledge_tables(connection: Connection) -> None:
    for table in reversed(KNOWLEDGE_TABLES):
        table.drop(connection, checkfirst=True)


def _create_pending_search_mode_table(connection: Connection) -> None:
    PENDING_SEARCH_MODE_TABLE.create(connection, checkfirst=True)


def _drop_pending_search_mode_table(connection: Connection) -> None:
    PENDING_SEARCH_MODE_TABLE.drop(connection, checkfirst=True)


async def _configure_capture_event_rls(
    connection: AsyncConnection, schema_name: str
) -> None:
    await _configure_user_space_rls(connection, schema_name, "capture_events")


async def _configure_user_space_rls(
    connection: AsyncConnection, schema_name: str, table_name: str
) -> None:
    table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
    policy_name = f"{table_name}_user_space_scope"
    await connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    await connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    await connection.execute(
        text(f"DROP POLICY IF EXISTS {_quote_identifier(policy_name)} ON {table}")
    )
    await connection.execute(
        text(
            f"CREATE POLICY {_quote_identifier(policy_name)} ON {table} "
            "USING (user_space_id = NULLIF("
            "current_setting('second_brain.user_space_id', true), ''"
            ")::uuid) "
            "WITH CHECK (user_space_id = NULLIF("
            "current_setting('second_brain.user_space_id', true), ''"
            ")::uuid)"
        )
    )


async def _grant_task_privileges(connection: AsyncConnection, schema_name: str) -> None:
    quoted_schema = _quote_identifier(schema_name)
    task_tables = (
        f"{quoted_schema}.tasks, {quoted_schema}.task_provenance, "
        f"{quoted_schema}.pending_capture_selections"
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {task_tables} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {task_tables} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            "GRANT UPDATE ON TABLE "
            f"{quoted_schema}.tasks, {quoted_schema}.pending_capture_selections "
            f"TO {APPLICATION_ROLE}"
        )
    )


async def _grant_knowledge_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    quoted_schema = _quote_identifier(schema_name)
    knowledge_tables = ", ".join(
        f"{quoted_schema}.{table_name}"
        for table_name in (
            "notes",
            "note_provenance",
            "ideas",
            "idea_provenance",
            "decisions",
            "decision_provenance",
            "questions",
            "question_provenance",
        )
    )
    await connection.execute(
        text(
            f"REVOKE ALL PRIVILEGES ON TABLE {knowledge_tables} FROM {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {knowledge_tables} TO {APPLICATION_ROLE}")
    )


async def _grant_retrieval_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    table = f'{_quote_identifier(schema_name)}."pending_search_modes"'
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} "
            f"TO {APPLICATION_ROLE}"
        )
    )


async def _create_full_text_indexes(
    connection: AsyncConnection, schema_name: str
) -> None:
    schema = _quote_identifier(schema_name)
    for index_name, table_name, column_name in (
        ("ix_notes_text_fts", "notes", "text"),
        ("ix_tasks_title_fts", "tasks", "title"),
        ("ix_ideas_text_fts", "ideas", "text"),
        ("ix_decisions_text_fts", "decisions", "text"),
        ("ix_questions_text_fts", "questions", "text"),
    ):
        await connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {_quote_identifier(index_name)} "
                f"ON {schema}.{_quote_identifier(table_name)} USING GIN "
                "(to_tsvector('simple'::regconfig, "
                f"{_quote_identifier(column_name)}))"
            )
        )


def _quote_identifier(value: str) -> str:
    return f'"{value.replace('"', '""')}"'

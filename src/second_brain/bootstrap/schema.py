from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.schema import Table

from second_brain.slices.capture.adapters.persistence.models import (
    CaptureEventModel,
    TelegramAttachmentModel,
)
from second_brain.slices.classification.adapters.persistence.models import (
    ClassificationResultModel,
)
from second_brain.slices.contacts.adapters.persistence.models import ContactModel
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
from second_brain.slices.memory.adapters.persistence.models import (
    MemoryAnswerModel,
    MemoryAnswerRunModel,
    MemoryAnswerSourceModel,
    MemoryAnswerStepModel,
    MemoryQuestionModel,
    MemoryRunEvidenceModel,
    PendingMemoryQuestionModel,
)
from second_brain.slices.processing.adapters.persistence.models import (
    NOTICE_KIND_CHECK_NAME,
    ProcessingNoticeModel,
    ProcessingRunModel,
    ProcessingStepModel,
    TranscriptModel,
)
from second_brain.slices.processing.domain.entities import ProcessingNoticeKind
from second_brain.slices.projects.adapters.persistence.models import (
    ProjectCaptureEventLinkModel,
    ProjectContextModel,
    ProjectDecisionLinkModel,
    ProjectIdeaLinkModel,
    ProjectModel,
    ProjectNoteLinkModel,
    ProjectQuestionLinkModel,
    ProjectTaskLinkModel,
)
from second_brain.slices.reminders.adapters.persistence.models import ReminderModel
from second_brain.slices.retrieval.adapters.persistence.models import (
    IndexingTargetModel,
    PendingSearchModeModel,
    SemanticDocumentModel,
)
from second_brain.slices.tasks.adapters.persistence.models import (
    PendingCaptureSelectionModel,
    TaskModel,
    TaskProvenanceModel,
)

CAPTURE_TABLES = (
    cast(Table, CaptureEventModel.__table__),
    cast(Table, TelegramAttachmentModel.__table__),
)
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
PROCESSING_TABLES = (
    cast(Table, ProcessingRunModel.__table__),
    cast(Table, ProcessingStepModel.__table__),
    cast(Table, TranscriptModel.__table__),
    cast(Table, ProcessingNoticeModel.__table__),
)
CLASSIFICATION_TABLES = (cast(Table, ClassificationResultModel.__table__),)
RETRIEVAL_TABLES = (
    cast(Table, PendingSearchModeModel.__table__),
    cast(Table, SemanticDocumentModel.__table__),
    cast(Table, IndexingTargetModel.__table__),
)
PROJECT_TABLES = (
    cast(Table, ProjectModel.__table__),
    cast(Table, ProjectContextModel.__table__),
    cast(Table, ProjectCaptureEventLinkModel.__table__),
    cast(Table, ProjectNoteLinkModel.__table__),
    cast(Table, ProjectTaskLinkModel.__table__),
    cast(Table, ProjectIdeaLinkModel.__table__),
    cast(Table, ProjectDecisionLinkModel.__table__),
    cast(Table, ProjectQuestionLinkModel.__table__),
)
MEMORY_TABLES = (
    cast(Table, PendingMemoryQuestionModel.__table__),
    cast(Table, MemoryQuestionModel.__table__),
    cast(Table, MemoryAnswerRunModel.__table__),
    cast(Table, MemoryAnswerStepModel.__table__),
    cast(Table, MemoryRunEvidenceModel.__table__),
    cast(Table, MemoryAnswerModel.__table__),
    cast(Table, MemoryAnswerSourceModel.__table__),
)
REMINDER_TABLES = (cast(Table, ReminderModel.__table__),)
CONTACT_TABLES = (cast(Table, ContactModel.__table__),)
MEMORY_TABLE_NAMES = (
    "pending_memory_questions",
    "memory_questions",
    "memory_answer_runs",
    "memory_answer_steps",
    "memory_run_evidence",
    "memory_answers",
    "memory_answer_sources",
)


async def _ensure_vector_extension(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


async def initialize_schema(engine: AsyncEngine, schema_name: str = "public") -> None:
    await _ensure_vector_extension(engine)
    await initialize_identity_schema(engine, schema_name)
    await _initialize_capture_schema(engine, schema_name)
    await _initialize_processing_schema(engine, schema_name)
    await _initialize_classification_schema(engine, schema_name)
    await _initialize_task_schema(engine, schema_name)
    await _initialize_knowledge_schema(engine, schema_name)
    await _initialize_project_schema(engine, schema_name)
    await _initialize_retrieval_schema(engine, schema_name)
    await _initialize_memory_schema(engine, schema_name)
    await _initialize_reminder_schema(engine, schema_name)
    await _initialize_contact_schema(engine, schema_name)


async def reset_prototype_schema(
    engine: AsyncEngine, confirm: bool, schema_name: str = "public"
) -> None:
    if not confirm:
        await reset_identity_prototype_schema(engine, confirm, schema_name)
        return
    await _ensure_vector_extension(engine)
    await _drop_contact_schema(engine)
    await _drop_reminder_schema(engine)
    await _drop_memory_schema(engine)
    await _drop_retrieval_schema(engine)
    await _drop_project_schema(engine)
    await _drop_task_schema(engine)
    await _drop_knowledge_schema(engine)
    await _drop_classification_schema(engine)
    await _drop_processing_schema(engine)
    await _drop_capture_schema(engine)
    await reset_identity_prototype_schema(engine, confirm, schema_name)
    await _initialize_capture_schema(engine, schema_name)
    await _initialize_processing_schema(engine, schema_name)
    await _initialize_classification_schema(engine, schema_name)
    await _initialize_task_schema(engine, schema_name)
    await _initialize_knowledge_schema(engine, schema_name)
    await _initialize_project_schema(engine, schema_name)
    await _initialize_retrieval_schema(engine, schema_name)
    await _initialize_memory_schema(engine, schema_name)
    await _initialize_reminder_schema(engine, schema_name)
    await _initialize_contact_schema(engine, schema_name)


async def _initialize_capture_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_capture_tables)
        for table_name in ("capture_events", "telegram_attachments"):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_capture_privileges(connection, schema_name)


async def _drop_capture_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_capture_tables)


async def _initialize_processing_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_processing_tables)
        await _reconcile_notice_kind_check(connection, schema_name)
        for table_name in (
            "processing_runs",
            "processing_steps",
            "transcripts",
            "processing_notices",
        ):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_processing_privileges(connection, schema_name)


async def _reconcile_notice_kind_check(
    connection: AsyncConnection, schema_name: str
) -> None:
    # create_all(checkfirst=True) skips an existing processing_notices table, so
    # a live prod DB keeps its OLD kind CHECK (success/failure only) and would
    # reject 'empty_voice'. Re-apply the current ORM definition idempotently:
    # harmless drop+add of the same predicate on a fresh DB, a repair on a live
    # one. The new set is a strict superset — no existing row violates it.
    expression = _notice_kind_check_expression()
    table = f"{_quote_identifier(schema_name)}.processing_notices"
    quoted_name = _quote_identifier(NOTICE_KIND_CHECK_NAME)
    await connection.execute(
        text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {quoted_name}")
    )
    await connection.execute(
        text(f"ALTER TABLE {table} ADD CONSTRAINT {quoted_name} CHECK ({expression})")
    )


def _notice_kind_check_expression() -> str:
    # Тот же единственный источник, из которого не-нативный Enum генерирует свой
    # CHECK: сами значения ProcessingNoticeKind.
    kinds = ", ".join(f"'{kind.value}'" for kind in ProcessingNoticeKind)
    return f"kind IN ({kinds})"


async def _drop_processing_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_processing_tables)


async def _initialize_classification_schema(
    engine: AsyncEngine, schema_name: str
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_classification_tables)
        await _configure_user_space_rls(
            connection, schema_name, "classification_results"
        )
        await _grant_classification_privileges(connection, schema_name)


async def _drop_classification_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_classification_tables)


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


async def _initialize_project_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_project_tables)
        for table_name in (
            "projects",
            "project_contexts",
            "project_capture_event_links",
            "project_note_links",
            "project_task_links",
            "project_idea_links",
            "project_decision_links",
            "project_question_links",
        ):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_project_privileges(connection, schema_name)


async def _drop_project_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_project_tables)


async def _initialize_retrieval_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_retrieval_tables)
        for table_name in (
            "pending_search_modes",
            "semantic_documents",
            "indexing_targets",
        ):
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_retrieval_privileges(connection, schema_name)
        await _create_full_text_indexes(connection, schema_name)


async def _drop_retrieval_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_retrieval_tables)


async def _initialize_memory_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_memory_tables)
        for table_name in MEMORY_TABLE_NAMES:
            await _configure_user_space_rls(connection, schema_name, table_name)
        await _grant_memory_privileges(connection, schema_name)


async def _drop_memory_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_memory_tables)


async def _initialize_reminder_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_reminder_tables)
        await _reconcile_reminder_telegram_message_id(connection, schema_name)
        await _configure_user_space_rls(connection, schema_name, "reminders")
        await _grant_reminder_privileges(connection, schema_name)


async def _reconcile_reminder_telegram_message_id(
    connection: AsyncConnection, schema_name: str
) -> None:
    # create_all(checkfirst=True) skips an existing reminders table, so a live
    # prod DB never gains the delivery-evidence column. Add it idempotently:
    # no-op on a fresh DB, a repair on a live one. Existing rows keep NULL
    # (sent before the column existed → no evidence), so the ADD is forward-only.
    table = f"{_quote_identifier(schema_name)}.reminders"
    await connection.execute(
        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT")
    )


async def _drop_reminder_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_reminder_tables)


async def _initialize_contact_schema(engine: AsyncEngine, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_contact_tables)
        await _configure_user_space_rls(connection, schema_name, "contacts")
        await _grant_contact_privileges(connection, schema_name)


async def _drop_contact_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_drop_contact_tables)


def _create_capture_tables(connection: Connection) -> None:
    for table in CAPTURE_TABLES:
        table.create(connection, checkfirst=True)


def _drop_capture_tables(connection: Connection) -> None:
    for table in reversed(CAPTURE_TABLES):
        table.drop(connection, checkfirst=True)


def _create_processing_tables(connection: Connection) -> None:
    for table in PROCESSING_TABLES:
        table.create(connection, checkfirst=True)


def _drop_processing_tables(connection: Connection) -> None:
    for table in reversed(PROCESSING_TABLES):
        table.drop(connection, checkfirst=True)


def _create_classification_tables(connection: Connection) -> None:
    for table in CLASSIFICATION_TABLES:
        table.create(connection, checkfirst=True)


def _drop_classification_tables(connection: Connection) -> None:
    for table in reversed(CLASSIFICATION_TABLES):
        table.drop(connection, checkfirst=True)


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


def _create_project_tables(connection: Connection) -> None:
    for table in PROJECT_TABLES:
        table.create(connection, checkfirst=True)


def _drop_project_tables(connection: Connection) -> None:
    for table in reversed(PROJECT_TABLES):
        table.drop(connection, checkfirst=True)


def _create_retrieval_tables(connection: Connection) -> None:
    for table in RETRIEVAL_TABLES:
        table.create(connection, checkfirst=True)


def _drop_retrieval_tables(connection: Connection) -> None:
    for table in reversed(RETRIEVAL_TABLES):
        table.drop(connection, checkfirst=True)


def _create_memory_tables(connection: Connection) -> None:
    for table in MEMORY_TABLES:
        table.create(connection, checkfirst=True)


def _drop_memory_tables(connection: Connection) -> None:
    for table in reversed(MEMORY_TABLES):
        table.drop(connection, checkfirst=True)


def _create_reminder_tables(connection: Connection) -> None:
    for table in REMINDER_TABLES:
        table.create(connection, checkfirst=True)


def _drop_reminder_tables(connection: Connection) -> None:
    for table in reversed(REMINDER_TABLES):
        table.drop(connection, checkfirst=True)


def _create_contact_tables(connection: Connection) -> None:
    for table in CONTACT_TABLES:
        table.create(connection, checkfirst=True)


def _drop_contact_tables(connection: Connection) -> None:
    for table in reversed(CONTACT_TABLES):
        table.drop(connection, checkfirst=True)


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


async def _grant_capture_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    schema = _quote_identifier(schema_name)
    capture_events = f'{schema}."capture_events"'
    attachments = f'{schema}."telegram_attachments"'
    tables = f"{capture_events}, {attachments}"
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {tables} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {tables} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            "GRANT UPDATE (storage_key, sha256, stored_size, stored_mime_type, "
            f"stored_at) ON TABLE {attachments} TO {APPLICATION_ROLE}"
        )
    )


async def _grant_processing_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    schema = _quote_identifier(schema_name)
    all_tables = ", ".join(
        f"{schema}.{_quote_identifier(table_name)}"
        for table_name in (
            "processing_runs",
            "processing_steps",
            "transcripts",
            "processing_notices",
        )
    )
    mutable_tables = ", ".join(
        f"{schema}.{_quote_identifier(table_name)}"
        for table_name in ("processing_steps", "processing_notices")
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {all_tables} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {all_tables} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT UPDATE ON TABLE {mutable_tables} TO {APPLICATION_ROLE}")
    )


async def _grant_classification_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    table = f'{_quote_identifier(schema_name)}."classification_results"'
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {table} TO {APPLICATION_ROLE}")
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
    schema = _quote_identifier(schema_name)
    pending_table = f'{schema}."pending_search_modes"'
    append_only_tables = f'{schema}."semantic_documents", {schema}."indexing_targets"'
    await connection.execute(
        text(
            f"REVOKE ALL PRIVILEGES ON TABLE {pending_table}, {append_only_tables} "
            f"FROM {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {pending_table} "
            f"TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT ON TABLE {append_only_tables} TO {APPLICATION_ROLE}"
        )
    )


async def _grant_memory_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    schema = _quote_identifier(schema_name)
    pending_table = f'{schema}."pending_memory_questions"'
    step_table = f'{schema}."memory_answer_steps"'
    append_only_tables = ", ".join(
        f"{schema}.{_quote_identifier(table_name)}"
        for table_name in (
            "memory_questions",
            "memory_answer_runs",
            "memory_run_evidence",
            "memory_answers",
            "memory_answer_sources",
        )
    )
    all_tables = f"{pending_table}, {step_table}, {append_only_tables}"
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {all_tables} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT ON TABLE {append_only_tables} TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE {step_table} TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {pending_table} "
            f"TO {APPLICATION_ROLE}"
        )
    )


async def _grant_reminder_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    table = f'{_quote_identifier(schema_name)}."reminders"'
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM {APPLICATION_ROLE}")
    )
    # create=INSERT, claim_due=SELECT, mark_sent/cancel_for_task=UPDATE. Без DELETE.
    await connection.execute(
        text(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO {APPLICATION_ROLE}")
    )


async def _grant_contact_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    table = f'{_quote_identifier(schema_name)}."contacts"'
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM {APPLICATION_ROLE}")
    )
    # upsert=INSERT+UPDATE (ON CONFLICT DO UPDATE), доставка=SELECT. Без DELETE.
    await connection.execute(
        text(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO {APPLICATION_ROLE}")
    )


async def _grant_project_privileges(
    connection: AsyncConnection, schema_name: str
) -> None:
    schema = _quote_identifier(schema_name)
    project_table = f'{schema}."projects"'
    context_table = f'{schema}."project_contexts"'
    link_tables = ", ".join(
        f"{schema}.{_quote_identifier(table_name)}"
        for table_name in (
            "project_capture_event_links",
            "project_note_links",
            "project_task_links",
            "project_idea_links",
            "project_decision_links",
            "project_question_links",
        )
    )
    all_tables = f"{project_table}, {context_table}, {link_tables}"
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON TABLE {all_tables} FROM {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {project_table} TO {APPLICATION_ROLE}")
    )
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE {context_table} "
            f"TO {APPLICATION_ROLE}"
        )
    )
    await connection.execute(
        text(f"GRANT SELECT, INSERT ON TABLE {link_tables} TO {APPLICATION_ROLE}")
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

from datetime import UTC, datetime
from uuid import UUID

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.knowledge.application.knowledge_capture import KnowledgeCapture
from second_brain.slices.knowledge.domain.entities import Decision, Idea, Note, Question
from second_brain.slices.knowledge.ports.repositories import (
    DecisionWriter,
    IdeaWriter,
    NoteWriter,
    QuestionWriter,
)
from second_brain.slices.tasks.application.contracts import (
    ConsumePendingTaskTextCommand,
    CreateTaskCommand,
    CreateTypedCaptureCommand,
)
from second_brain.slices.tasks.application.task_capture import TaskCapture
from second_brain.slices.tasks.domain.entities import (
    PendingCaptureType,
    Task,
    TaskStatus,
)
from second_brain.slices.tasks.ports.repositories import (
    PendingCaptureSelectionStore,
    TaskWriter,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
ACCESS = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
SOURCE = UUID("00000000-0000-0000-0000-000000000101")


class InMemoryPendingSelection(PendingCaptureSelectionStore):
    def __init__(self, selection: PendingCaptureType | None = None) -> None:
        self.selection = selection

    async def consume_selection(
        self, command: ConsumePendingTaskTextCommand
    ) -> PendingCaptureType:
        selection = self.selection or PendingCaptureType.NOTE
        self.selection = PendingCaptureType.NOTE
        return selection

    async def consume_awaiting_task(
        self, command: ConsumePendingTaskTextCommand
    ) -> Task | None:
        return None


class InMemoryTaskWriter(TaskWriter):
    def __init__(self) -> None:
        self.commands: list[CreateTaskCommand] = []

    async def create(self, command: CreateTaskCommand) -> Task:
        self.commands.append(command)
        return Task(
            id=UUID("00000000-0000-0000-0000-000000000201"),
            user_space_id=command.access_context.user_space_id,
            title=command.title,
            description=None,
            status=TaskStatus.INBOX,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )


class InMemoryKnowledgeWriter(NoteWriter, IdeaWriter, DecisionWriter, QuestionWriter):
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def create(self, command: object) -> object:
        self.commands.append(command)
        entity_type = {
            "CreateNoteCommand": Note,
            "CreateIdeaCommand": Idea,
            "CreateDecisionCommand": Decision,
            "CreateQuestionCommand": Question,
        }[type(command).__name__]
        return entity_type(
            id=UUID("00000000-0000-0000-0000-000000000202"),
            user_space_id=command.access_context.user_space_id,
            text=command.text,
            source_capture_event_id=command.source_capture_event_id,
            created_at=command.created_at,
            updated_at=command.created_at,
            trace_id=command.trace_id,
        )


def command(text: str | None = "  exact typed text  ") -> ConsumePendingTaskTextCommand:
    return ConsumePendingTaskTextCommand(
        access_context=ACCESS,
        text=text,
        is_private_chat=True,
        telegram_message_id=501,
        source_capture_event_id=SOURCE,
        created_at=NOW,
        trace_id="1" * 32,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "expected_type"),
    [
        (None, Note),
        (PendingCaptureType.NOTE, Note),
        (PendingCaptureType.TASK, Task),
        (PendingCaptureType.IDEA, Idea),
        (PendingCaptureType.DECISION, Decision),
        (PendingCaptureType.QUESTION, Question),
    ],
)
async def test_eligible_text_routes_exactly_to_pending_type_then_resets_to_note(
    selection: PendingCaptureType | None, expected_type: type[object]
) -> None:
    pending = InMemoryPendingSelection(selection)
    task_writer = InMemoryTaskWriter()
    knowledge_writer = InMemoryKnowledgeWriter()
    capture = TaskCapture(
        pending,
        task_writer,
        KnowledgeCapture(
            note_writer=knowledge_writer,
            idea_writer=knowledge_writer,
            decision_writer=knowledge_writer,
            question_writer=knowledge_writer,
        ),
    )

    record = await capture.consume_for_text(command())

    assert isinstance(record, expected_type)
    assert pending.selection is PendingCaptureType.NOTE
    assert len(task_writer.commands) + len(knowledge_writer.commands) == 1
    assert record.user_space_id == ACCESS.user_space_id
    assert record.source_capture_event_id == SOURCE
    assert record.trace_id == "1" * 32
    assert (
        record.title if isinstance(record, Task) else record.text
    ) == "  exact typed text  "


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", "  /command"])
async def test_ineligible_text_preserves_selected_type(text: str | None) -> None:
    pending = InMemoryPendingSelection(PendingCaptureType.DECISION)
    task_writer = InMemoryTaskWriter()
    knowledge_writer = InMemoryKnowledgeWriter()
    capture = TaskCapture(
        pending,
        task_writer,
        KnowledgeCapture(
            note_writer=knowledge_writer,
            idea_writer=knowledge_writer,
            decision_writer=knowledge_writer,
            question_writer=knowledge_writer,
        ),
    )

    result = await capture.consume_for_text(command(text))

    assert result is None
    assert pending.selection is PendingCaptureType.DECISION
    assert task_writer.commands == []
    assert knowledge_writer.commands == []


@pytest.mark.asyncio
async def test_frozen_selection_can_create_record_without_consuming_live_mode() -> None:
    pending = InMemoryPendingSelection(PendingCaptureType.TASK)
    task_writer = InMemoryTaskWriter()
    knowledge_writer = InMemoryKnowledgeWriter()
    capture = TaskCapture(
        pending,
        task_writer,
        KnowledgeCapture(
            note_writer=knowledge_writer,
            idea_writer=knowledge_writer,
            decision_writer=knowledge_writer,
            question_writer=knowledge_writer,
        ),
    )

    result = await capture.create_for_selection(
        CreateTypedCaptureCommand(
            access_context=ACCESS,
            selection=PendingCaptureType.IDEA,
            text="voice transcript",
            source_capture_event_id=SOURCE,
            created_at=NOW,
            trace_id="2" * 32,
        )
    )

    assert isinstance(result, Idea)
    assert pending.selection is PendingCaptureType.TASK
    assert len(knowledge_writer.commands) == 1
    assert task_writer.commands == []

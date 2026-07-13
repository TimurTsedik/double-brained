from second_brain.slices.knowledge.application.contracts import (
    CreateDecisionCommand,
    CreateIdeaCommand,
    CreateNoteCommand,
    CreateQuestionCommand,
)
from second_brain.slices.knowledge.domain.entities import (
    Decision,
    Idea,
    Note,
    Question,
)
from second_brain.slices.knowledge.ports.repositories import (
    DecisionWriter,
    IdeaWriter,
    NoteWriter,
    QuestionWriter,
)


class KnowledgeCapture:
    """Creates typed knowledge records through injected slice ports."""

    def __init__(
        self,
        note_writer: NoteWriter,
        idea_writer: IdeaWriter,
        decision_writer: DecisionWriter,
        question_writer: QuestionWriter,
    ) -> None:
        self._note_writer = note_writer
        self._idea_writer = idea_writer
        self._decision_writer = decision_writer
        self._question_writer = question_writer

    async def create_note(self, command: CreateNoteCommand) -> Note:
        return await self._note_writer.create(command)

    async def create_idea(self, command: CreateIdeaCommand) -> Idea:
        return await self._idea_writer.create(command)

    async def create_decision(self, command: CreateDecisionCommand) -> Decision:
        return await self._decision_writer.create(command)

    async def create_question(self, command: CreateQuestionCommand) -> Question:
        return await self._question_writer.create(command)

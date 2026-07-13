from typing import Protocol

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


class NoteWriter(Protocol):
    async def create(self, command: CreateNoteCommand) -> Note: ...


class IdeaWriter(Protocol):
    async def create(self, command: CreateIdeaCommand) -> Idea: ...


class DecisionWriter(Protocol):
    async def create(self, command: CreateDecisionCommand) -> Decision: ...


class QuestionWriter(Protocol):
    async def create(self, command: CreateQuestionCommand) -> Question: ...

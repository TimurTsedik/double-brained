from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    CancelProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    SelectProjectCommand,
)
from second_brain.slices.projects.domain.entities import Project


class ProjectStore(Protocol):
    async def set_awaiting_creation(
        self, command: BeginProjectCreationCommand
    ) -> None: ...

    async def cancel_awaiting_creation(
        self, command: CancelProjectCreationCommand
    ) -> None: ...

    async def lock_awaiting_creation(self, access_context: AccessContext) -> bool: ...

    async def create_or_select(
        self,
        command: ConsumeProjectNameCommand,
        name: str,
        name_key: str,
    ) -> None: ...

    async def list_projects(
        self, access_context: AccessContext
    ) -> tuple[Project, ...]: ...

    async def get_current_project_id(
        self, access_context: AccessContext
    ) -> UUID | None: ...

    async def select(self, command: SelectProjectCommand) -> bool: ...

    async def clear(self, command: ClearCurrentProjectCommand) -> bool: ...

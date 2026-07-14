from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    CancelProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    ProjectListItem,
    ProjectPanelResult,
    SelectProjectCommand,
)
from second_brain.slices.projects.ports.repositories import ProjectStore


class Projects:
    def __init__(self, store: ProjectStore) -> None:
        self._store = store

    async def begin_creation(self, command: BeginProjectCreationCommand) -> None:
        await self._store.set_awaiting_creation(command)

    async def cancel_creation(self, command: CancelProjectCreationCommand) -> None:
        await self._store.cancel_awaiting_creation(command)

    async def consume_name(
        self, command: ConsumeProjectNameCommand
    ) -> ProjectPanelResult | None:
        if not await self._store.lock_awaiting_creation(command.access_context):
            return None
        name = command.name.strip()
        if not name:
            return await self._panel(
                command.access_context,
                action_succeeded=False,
                name_required=True,
            )
        await self._store.create_or_select(command, name, name.casefold())
        return await self._panel(command.access_context, action_succeeded=True)

    async def list_projects(self, access_context: AccessContext) -> ProjectPanelResult:
        return await self._panel(access_context, action_succeeded=None)

    async def select(self, command: SelectProjectCommand) -> ProjectPanelResult:
        succeeded = await self._store.select(command)
        return await self._panel(command.access_context, action_succeeded=succeeded)

    async def clear(self, command: ClearCurrentProjectCommand) -> ProjectPanelResult:
        changed = await self._store.clear(command)
        return await self._panel(command.access_context, action_succeeded=changed)

    async def _panel(
        self,
        access_context: AccessContext,
        action_succeeded: bool | None,
        name_required: bool = False,
    ) -> ProjectPanelResult:
        projects = await self._store.list_projects(access_context)
        return ProjectPanelResult(
            items=tuple(ProjectListItem(item.id, item.name) for item in projects),
            current_project_id=await self._store.get_current_project_id(access_context),
            action_succeeded=action_succeeded,
            name_required=name_required,
        )

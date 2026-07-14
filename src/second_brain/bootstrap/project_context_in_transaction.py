from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.projects.adapters.persistence.repository import (
    PostgresProjectContentLinkWriter,
    PostgresProjectWriter,
)
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    CancelProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    InheritCaptureProjectLinksCommand,
    LinkCurrentProjectToCaptureCommand,
    LinkProjectContentCommand,
    ProjectContentLinkPort,
    ProjectPanelPort,
    ProjectPanelResult,
    SelectProjectCommand,
)
from second_brain.slices.projects.application.projects import Projects


class ProjectContextInTransaction(ProjectPanelPort, ProjectContentLinkPort):
    async def list_projects(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> ProjectPanelResult:
        return await _projects(transaction).list_projects(access_context)

    async def begin_creation(
        self, command: BeginProjectCreationCommand, transaction: UpdateTransaction
    ) -> None:
        await _projects(transaction).begin_creation(command)

    async def cancel_creation(
        self, command: CancelProjectCreationCommand, transaction: UpdateTransaction
    ) -> None:
        await _projects(transaction).cancel_creation(command)

    async def consume_name(
        self, command: ConsumeProjectNameCommand, transaction: UpdateTransaction
    ) -> ProjectPanelResult | None:
        return await _projects(transaction).consume_name(command)

    async def select(
        self, command: SelectProjectCommand, transaction: UpdateTransaction
    ) -> ProjectPanelResult:
        return await _projects(transaction).select(command)

    async def clear(
        self, command: ClearCurrentProjectCommand, transaction: UpdateTransaction
    ) -> ProjectPanelResult:
        return await _projects(transaction).clear(command)

    async def link_current_to_capture(
        self,
        command: LinkCurrentProjectToCaptureCommand,
        transaction: UpdateTransaction,
    ) -> None:
        await _links(transaction).link_current_to_capture(command)

    async def inherit_capture_links(
        self,
        command: InheritCaptureProjectLinksCommand,
        transaction: UpdateTransaction,
    ) -> None:
        await _links(transaction).inherit_capture_links(command)

    async def link(
        self, command: LinkProjectContentCommand, transaction: UpdateTransaction
    ) -> bool:
        return await _links(transaction).link(command)


def _active_session(transaction: UpdateTransaction) -> AsyncSession:
    if not isinstance(transaction, PostgresUpdateTransaction):
        raise TypeError("project context requires the PostgreSQL update transaction")
    return transaction.active_session


def _projects(transaction: UpdateTransaction) -> Projects:
    return Projects(PostgresProjectWriter(_active_session(transaction)))


def _links(transaction: UpdateTransaction) -> PostgresProjectContentLinkWriter:
    return PostgresProjectContentLinkWriter(_active_session(transaction))

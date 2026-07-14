from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from second_brain.slices.identity.application.contracts import (
    AccessContext,
    UpdateTransaction,
)
from second_brain.slices.projects.domain.entities import ProjectContentKind


@dataclass(frozen=True)
class BeginProjectCreationCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class CancelProjectCreationCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ConsumeProjectNameCommand:
    access_context: AccessContext
    name: str = field(repr=False)
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class SelectProjectCommand:
    access_context: AccessContext
    project_id: UUID
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ClearCurrentProjectCommand:
    access_context: AccessContext
    updated_at: datetime
    trace_id: str


@dataclass(frozen=True)
class LinkCurrentProjectToCaptureCommand:
    access_context: AccessContext
    capture_event_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class InheritCaptureProjectLinksCommand:
    access_context: AccessContext
    source_capture_event_id: UUID
    content_kind: ProjectContentKind
    content_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class LinkProjectContentCommand:
    access_context: AccessContext
    project_id: UUID
    content_kind: ProjectContentKind
    content_id: UUID
    created_at: datetime
    trace_id: str


@dataclass(frozen=True)
class ProjectListItem:
    id: UUID
    name: str = field(repr=False)


@dataclass(frozen=True)
class ProjectPanelResult:
    items: tuple[ProjectListItem, ...]
    current_project_id: UUID | None
    action_succeeded: bool | None
    name_required: bool = False


class ProjectPanelPort(Protocol):
    async def list_projects(
        self, access_context: AccessContext, transaction: UpdateTransaction
    ) -> ProjectPanelResult: ...

    async def begin_creation(
        self, command: BeginProjectCreationCommand, transaction: UpdateTransaction
    ) -> None: ...

    async def cancel_creation(
        self, command: CancelProjectCreationCommand, transaction: UpdateTransaction
    ) -> None: ...

    async def consume_name(
        self, command: ConsumeProjectNameCommand, transaction: UpdateTransaction
    ) -> ProjectPanelResult | None: ...

    async def select(
        self, command: SelectProjectCommand, transaction: UpdateTransaction
    ) -> ProjectPanelResult: ...

    async def clear(
        self, command: ClearCurrentProjectCommand, transaction: UpdateTransaction
    ) -> ProjectPanelResult: ...


class ProjectContentLinkPort(Protocol):
    async def link_current_to_capture(
        self,
        command: LinkCurrentProjectToCaptureCommand,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def inherit_capture_links(
        self,
        command: InheritCaptureProjectLinksCommand,
        transaction: UpdateTransaction,
    ) -> None: ...

    async def link(
        self, command: LinkProjectContentCommand, transaction: UpdateTransaction
    ) -> bool: ...

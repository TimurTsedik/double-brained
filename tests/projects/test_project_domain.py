from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.projects.application.contracts import (
    BeginProjectCreationCommand,
    CancelProjectCreationCommand,
    ClearCurrentProjectCommand,
    ConsumeProjectNameCommand,
    SelectProjectCommand,
)
from second_brain.slices.projects.application.projects import Projects
from second_brain.slices.projects.domain.entities import Project, ProjectContentKind
from second_brain.slices.projects.ports.repositories import ProjectStore

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
TRACE_ID = "1" * 32
ACCESS_A = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
)
ACCESS_B = AccessContext(
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    user_space_id=UUID("00000000-0000-0000-0000-000000000022"),
)


class InMemoryProjectStore(ProjectStore):
    def __init__(self) -> None:
        self.awaiting: set[UUID] = set()
        self.projects: dict[UUID, list[Project]] = {}
        self.current: dict[UUID, UUID] = {}
        self.created_names: list[tuple[str, str]] = []

    async def set_awaiting_creation(self, command: BeginProjectCreationCommand) -> None:
        self.awaiting.add(command.access_context.user_space_id)

    async def cancel_awaiting_creation(
        self, command: CancelProjectCreationCommand
    ) -> None:
        self.awaiting.discard(command.access_context.user_space_id)

    async def lock_awaiting_creation(self, access_context: AccessContext) -> bool:
        return access_context.user_space_id in self.awaiting

    async def create_or_select(
        self,
        command: ConsumeProjectNameCommand,
        name: str,
        name_key: str,
    ) -> None:
        space_id = command.access_context.user_space_id
        records = self.projects.setdefault(space_id, [])
        existing = next(
            (project for project in records if project.name.casefold() == name_key),
            None,
        )
        if existing is None:
            existing = Project(
                id=uuid5(space_id, name_key),
                user_space_id=space_id,
                name=name,
                created_at=command.created_at,
                updated_at=command.created_at,
                trace_id=command.trace_id,
            )
            records.append(existing)
            self.created_names.append((name, name_key))
        self.current[space_id] = existing.id
        self.awaiting.discard(space_id)

    async def list_projects(self, access_context: AccessContext) -> tuple[Project, ...]:
        return tuple(self.projects.get(access_context.user_space_id, ()))

    async def get_current_project_id(
        self, access_context: AccessContext
    ) -> UUID | None:
        return self.current.get(access_context.user_space_id)

    async def select(self, command: SelectProjectCommand) -> bool:
        space_id = command.access_context.user_space_id
        exists = any(
            project.id == command.project_id
            for project in self.projects.get(space_id, ())
        )
        if exists:
            self.current[space_id] = command.project_id
        return exists

    async def clear(self, command: ClearCurrentProjectCommand) -> bool:
        space_id = command.access_context.user_space_id
        changed = space_id in self.current
        self.current.pop(space_id, None)
        return changed


def begin(access_context: AccessContext = ACCESS_A) -> BeginProjectCreationCommand:
    return BeginProjectCreationCommand(access_context, NOW, TRACE_ID)


def name_command(
    name: str, access_context: AccessContext = ACCESS_A
) -> ConsumeProjectNameCommand:
    return ConsumeProjectNameCommand(access_context, name, NOW, TRACE_ID)


@pytest.mark.asyncio
async def test_create_trims_name_selects_project_and_hides_name_from_repr() -> None:
    store = InMemoryProjectStore()
    projects = Projects(store)
    await projects.begin_creation(begin())

    result = await projects.consume_name(name_command("  Second Brain  "))

    assert result is not None
    assert result.action_succeeded is True
    assert result.name_required is False
    assert len(result.items) == 1
    assert result.items[0].name == "Second Brain"
    assert result.current_project_id == result.items[0].id
    assert store.created_names == [("Second Brain", "second brain")]
    assert "Second Brain" not in repr(name_command("Second Brain"))
    assert "Second Brain" not in repr(result)
    assert "Second Brain" not in repr((await store.list_projects(ACCESS_A))[0])


@pytest.mark.asyncio
async def test_text_is_not_consumed_when_project_name_is_not_pending() -> None:
    result = await Projects(InMemoryProjectStore()).consume_name(
        name_command("ordinary note")
    )

    assert result is None


@pytest.mark.asyncio
async def test_blank_name_keeps_creation_mode_and_requests_name_again() -> None:
    store = InMemoryProjectStore()
    projects = Projects(store)
    await projects.begin_creation(begin())

    result = await projects.consume_name(name_command(" \n\t "))

    assert result is not None
    assert result.action_succeeded is False
    assert result.name_required is True
    assert ACCESS_A.user_space_id in store.awaiting
    assert store.projects == {}


@pytest.mark.asyncio
async def test_cancel_creation_uses_observable_mutation_command() -> None:
    store = InMemoryProjectStore()
    projects = Projects(store)
    await projects.begin_creation(begin())

    await projects.cancel_creation(
        CancelProjectCreationCommand(ACCESS_A, NOW, TRACE_ID)
    )

    assert ACCESS_A.user_space_id not in store.awaiting


@pytest.mark.asyncio
async def test_case_insensitive_duplicate_selects_existing_project() -> None:
    store = InMemoryProjectStore()
    projects = Projects(store)
    await projects.begin_creation(begin())
    first = await projects.consume_name(name_command("Second Brain"))
    await projects.begin_creation(begin())

    second = await projects.consume_name(name_command("second brain"))

    assert first is not None and second is not None
    assert len(second.items) == 1
    assert second.current_project_id == first.current_project_id
    assert store.created_names == [("Second Brain", "second brain")]


@pytest.mark.asyncio
async def test_selection_and_clear_are_sticky_and_scoped_to_user_space() -> None:
    store = InMemoryProjectStore()
    projects = Projects(store)
    for access, name in ((ACCESS_A, "A project"), (ACCESS_B, "B project")):
        await projects.begin_creation(begin(access))
        await projects.consume_name(name_command(name, access))

    project_a = (await store.list_projects(ACCESS_A))[0]
    project_b = (await store.list_projects(ACCESS_B))[0]
    rejected = await projects.select(
        SelectProjectCommand(ACCESS_A, project_b.id, NOW, TRACE_ID)
    )

    assert rejected.action_succeeded is False
    assert rejected.current_project_id == project_a.id
    listed = await projects.list_projects(ACCESS_A)
    assert listed.current_project_id == project_a.id

    cleared = await projects.clear(ClearCurrentProjectCommand(ACCESS_A, NOW, TRACE_ID))
    assert cleared.action_succeeded is True
    assert cleared.current_project_id is None
    assert (await projects.list_projects(ACCESS_B)).current_project_id == project_b.id


def test_content_kind_is_closed_to_current_typed_records() -> None:
    assert {kind.value for kind in ProjectContentKind} == {
        "capture_event",
        "note",
        "task",
        "idea",
        "decision",
        "question",
    }

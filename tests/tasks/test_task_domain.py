from datetime import UTC, datetime
from uuid import UUID

from second_brain.slices.tasks.domain.entities import Task, TaskStatus


def test_task_preserves_exact_title_and_starts_in_inbox() -> None:
    submitted_text = "  Купить молоко  "
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    task = Task(
        id=UUID("00000000-0000-0000-0000-000000000101"),
        user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
        title=submitted_text,
        description=None,
        status=TaskStatus.INBOX,
        source_capture_event_id=UUID("00000000-0000-0000-0000-000000000201"),
        created_at=created_at,
        updated_at=created_at,
        trace_id="1" * 32,
    )

    assert task.title == submitted_text
    assert task.description is None
    assert task.status is TaskStatus.INBOX


def test_task_repr_does_not_include_submitted_title() -> None:
    submitted_text = "private task text"
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    task = Task(
        id=UUID("00000000-0000-0000-0000-000000000101"),
        user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
        title=submitted_text,
        description=None,
        status=TaskStatus.INBOX,
        source_capture_event_id=UUID("00000000-0000-0000-0000-000000000201"),
        created_at=created_at,
        updated_at=created_at,
        trace_id="1" * 32,
    )

    assert submitted_text not in repr(task)

from datetime import UTC, datetime
from uuid import UUID

import pytest

from second_brain.slices.knowledge.domain.entities import (
    Decision,
    Idea,
    Note,
    Question,
)


@pytest.mark.parametrize("record_type", [Note, Idea, Decision, Question])
def test_knowledge_record_preserves_exact_submitted_text_and_hides_it_in_repr(
    record_type: type[Note | Idea | Decision | Question],
) -> None:
    submitted_text = "  Private, exact text  "
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    record = record_type(
        id=UUID("00000000-0000-0000-0000-000000000101"),
        user_space_id=UUID("00000000-0000-0000-0000-000000000011"),
        text=submitted_text,
        source_capture_event_id=UUID("00000000-0000-0000-0000-000000000201"),
        created_at=created_at,
        updated_at=created_at,
        trace_id="1" * 32,
    )

    assert record.text == submitted_text
    assert submitted_text not in repr(record)

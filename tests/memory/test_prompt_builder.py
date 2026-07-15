import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from second_brain.slices.memory.application import prompt_builder
from second_brain.slices.memory.application.contracts import LabelledSnippet
from second_brain.slices.memory.application.prompt_builder import build_reasoning_prompt
from second_brain.slices.memory.domain.entities import (
    EvidenceSnippet,
    MemoryRecordKind,
)

_CREATED = datetime(2026, 7, 15, tzinfo=UTC)


def snippet(
    text: str,
    kind: MemoryRecordKind = MemoryRecordKind.NOTE,
    label: str = "S1",
) -> EvidenceSnippet:
    return EvidenceSnippet(
        label=label,
        record_kind=kind,
        record_id=uuid4(),
        source_capture_event_id=uuid4(),
        created_at=_CREATED,
        text=text,
    )


def test_labels_are_carried_through_from_the_snapshot() -> None:
    first = snippet("alpha", MemoryRecordKind.NOTE, label="S1")
    second = snippet("beta", MemoryRecordKind.TASK, label="S2")
    third = snippet("gamma", MemoryRecordKind.DECISION, label="S3")

    labelled, label_map = build_reasoning_prompt("вопрос", (first, second, third))

    assert [item.label for item in labelled] == ["S1", "S2", "S3"]
    assert [item.text for item in labelled] == ["alpha", "beta", "gamma"]
    assert set(label_map) == {"S1", "S2", "S3"}
    assert label_map["S1"] == (
        first.record_kind,
        first.record_id,
        first.source_capture_event_id,
        first.created_at,
    )
    assert label_map["S3"] == (
        third.record_kind,
        third.record_id,
        third.source_capture_event_id,
        third.created_at,
    )


def test_only_label_and_text_reach_the_model() -> None:
    labelled, _ = build_reasoning_prompt("вопрос", (snippet("secret snippet body"),))

    assert all(isinstance(item, LabelledSnippet) for item in labelled)
    assert "secret snippet body" not in repr(labelled)


def test_empty_snapshot_gives_empty_prompt() -> None:
    labelled, label_map = build_reasoning_prompt("вопрос", ())

    assert labelled == ()
    assert label_map == {}


def test_builder_preserves_non_sequential_snapshot_labels() -> None:
    # A numerically-ordered snapshot reaches S10; the builder must keep it as-is,
    # not renumber it back to S2 by position.
    ninth = snippet("nine", label="S9")
    tenth = snippet("ten", label="S10")

    labelled, label_map = build_reasoning_prompt("вопрос", (ninth, tenth))

    assert [item.label for item in labelled] == ["S9", "S10"]
    assert set(label_map) == {"S9", "S10"}
    assert label_map["S10"][1] == tenth.record_id


def test_builder_is_pure() -> None:
    snippets = (snippet("alpha", label="S1"), snippet("beta", label="S2"))

    assert build_reasoning_prompt("q", snippets) == build_reasoning_prompt(
        "q", snippets
    )


def test_builder_module_does_not_reach_storage_or_network() -> None:
    source = Path(prompt_builder.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.partition(".")[0])

    assert {"sqlalchemy", "asyncpg", "aiogram", "urllib"}.isdisjoint(imported)

import json

import pytest

from second_brain.slices.memory.application.structured_output import (
    PROMPT_VERSION,
    REASONING_SYSTEM_PROMPT,
    RESPONSE_SCHEMA,
    SCHEMA_VERSION,
    ReasoningContractError,
    parse_reasoning_content,
)
from second_brain.slices.memory.domain.entities import EvidenceLevel

ALLOWED = ("S1", "S2")


def content(**payload: object) -> str:
    return json.dumps(payload)


def test_versions_and_schema_are_fixed() -> None:
    assert PROMPT_VERSION == "grounded-answer-v1"
    assert SCHEMA_VERSION == "grounded-answer-v1"
    assert REASONING_SYSTEM_PROMPT.strip() != ""
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    properties = RESPONSE_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties["evidence_level"]["enum"] == [
        level.value for level in EvidenceLevel
    ]


def test_valid_direct_answer_is_parsed() -> None:
    parsed = parse_reasoning_content(
        content(evidence_level="direct", answer="Ответ", source_labels=["S1", "S2"]),
        ALLOWED,
    )

    assert parsed.evidence_level is EvidenceLevel.DIRECT
    assert parsed.answer == "Ответ"
    assert parsed.source_labels == ("S1", "S2")


def test_insufficient_without_labels_is_accepted() -> None:
    parsed = parse_reasoning_content(
        content(evidence_level="insufficient", answer="", source_labels=[]),
        ALLOWED,
    )

    assert parsed.evidence_level is EvidenceLevel.INSUFFICIENT
    assert parsed.source_labels == ()


@pytest.mark.parametrize(
    "raw",
    [
        content(evidence_level="guess", answer="A", source_labels=["S1"]),
        content(evidence_level="direct", answer="A", source_labels=["S9"]),
        content(evidence_level="direct", answer="A", source_labels=["S1", "S1"]),
        content(evidence_level="insufficient", answer="", source_labels=["S1"]),
        content(evidence_level="insufficient", answer="", source_labels=["S9"]),
        content(evidence_level="insufficient", answer="", source_labels=["S1", "S1"]),
        content(evidence_level="direct", answer="A", source_labels=[]),
        content(
            evidence_level="direct",
            answer="A",
            source_labels=["S1"],
            extra="nope",
        ),
        content(evidence_level="direct", answer="A"),
        content(evidence_level="direct", answer="A", source_labels=[1]),
        content(evidence_level="direct", answer=5, source_labels=["S1"]),
        "[]",
        '"a string"',
        '{"evidence_level":"direct","answer":NaN,"source_labels":["S1"]}',
        "{not json",
    ],
)
def test_malformed_answers_are_rejected(raw: str) -> None:
    with pytest.raises(ReasoningContractError):
        parse_reasoning_content(raw, ALLOWED)


def test_contract_error_carries_no_answer_content() -> None:
    try:
        parse_reasoning_content(
            content(
                evidence_level="guess",
                answer="SECRET ANSWER CONTENT",
                source_labels=["S1"],
            ),
            ALLOWED,
        )
    except ReasoningContractError as error:
        assert "SECRET ANSWER CONTENT" not in str(error)
        assert error.safe_error_code == "reasoning_contract_violation"
    else:  # pragma: no cover - guard
        raise AssertionError("expected ReasoningContractError")

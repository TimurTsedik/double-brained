import json

import pytest

from second_brain.slices.classification.adapters.structured_output import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    parse_candidate_content,
)
from second_brain.slices.classification.domain.entities import (
    CandidateModality,
    CandidateType,
)


def test_schema_encodes_only_allowed_type_modality_pairs() -> None:
    items = RESPONSE_SCHEMA["properties"]["items"]
    branches = items["items"]["oneOf"]

    pairs = {
        branch["properties"]["type"]["const"]: tuple(
            branch["properties"]["modality"]["enum"]
        )
        for branch in branches
    }

    assert PROMPT_VERSION == "atomic-extraction-v3"
    assert SCHEMA_VERSION == "atomic-candidates-v2"
    assert "Верни только JSON без пояснений и Markdown" in SYSTEM_PROMPT
    assert "Не возвращай пустой items" in SYSTEM_PROMPT
    assert items["maxItems"] == 8
    assert pairs == {
        "note": ("observation", "completed_action"),
        "task": ("commitment",),
        "idea": ("suggestion", "hypothesis"),
        "decision": ("decision",),
        "question": ("question",),
    }


def test_parser_keeps_valid_siblings_and_counts_invalid_items() -> None:
    content = json.dumps(
        {
            "items": [
                {
                    "type": "task",
                    "source_quote": "Надо проверить",
                    "modality": "commitment",
                    "confidence": 0.95,
                },
                {
                    "type": "invented",
                    "source_quote": "invalid",
                    "modality": "observation",
                    "confidence": 1,
                },
            ]
        },
        ensure_ascii=False,
    )

    candidates, discarded = parse_candidate_content(content)

    assert discarded == 1
    assert len(candidates) == 1
    assert candidates[0].candidate_type is CandidateType.TASK
    assert candidates[0].modality is CandidateModality.COMMITMENT
    assert candidates[0].confidence == 0.95


@pytest.mark.parametrize(
    "content",
    [
        "provider prose",
        '{"items": [], "unexpected": true}',
        "[]",
    ],
)
def test_parser_rejects_non_contract_content(content: str) -> None:
    with pytest.raises(ValueError):
        parse_candidate_content(content)

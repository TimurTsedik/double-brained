from math import nan

import pytest

from second_brain.slices.classification.application.contracts import (
    ClassificationDraft,
    ClassificationRequest,
    ClassificationSource,
)
from second_brain.slices.classification.application.extraction import ClassifySource
from second_brain.slices.classification.domain.entities import (
    CandidateDisposition,
    CandidateModality,
    CandidateType,
    CandidateValidationCode,
    ClassificationCandidateDraft,
)


class FakeModel:
    def __init__(self, draft: ClassificationDraft) -> None:
        self.draft = draft
        self.requests: list[ClassificationRequest] = []

    async def classify(self, request: ClassificationRequest) -> ClassificationDraft:
        self.requests.append(request)
        return self.draft


def candidate(
    candidate_type: CandidateType,
    quote: str,
    modality: CandidateModality,
    confidence: float = 0.95,
) -> ClassificationCandidateDraft:
    return ClassificationCandidateDraft(
        candidate_type=candidate_type,
        source_quote=quote,
        modality=modality,
        confidence=confidence,
    )


def model_with(*candidates: ClassificationCandidateDraft) -> FakeModel:
    return FakeModel(
        ClassificationDraft(
            model_name="local-test-model",
            prompt_version="prompt-v1",
            schema_version="schema-v1",
            candidates=candidates,
            discarded_candidate_count=0,
        )
    )


@pytest.mark.asyncio
async def test_exact_high_confidence_task_is_selected_for_materialization() -> None:
    model = model_with(
        candidate(
            CandidateType.TASK,
            "Надо позвонить Сергею",
            CandidateModality.COMMITMENT,
        )
    )

    outcome = await ClassifySource(model).execute(
        ClassificationSource(
            text="Надо позвонить Сергею. PostgreSQL поддерживает FTS.",
            base_type=CandidateType.NOTE,
        )
    )

    assert outcome.skipped_reason is None
    assert outcome.model_name == "local-test-model"
    assert len(outcome.source_sha256) == 64
    assert [item.disposition for item in outcome.candidates] == [
        CandidateDisposition.MATERIALIZE
    ]
    assert [item.validation_code for item in outcome.candidates] == [
        CandidateValidationCode.VALID
    ]
    assert model.requests[0].source_text == (
        "Надо позвонить Сергею. PostgreSQL поддерживает FTS."
    )
    assert "Надо позвонить" not in repr(model.requests[0])


@pytest.mark.asyncio
async def test_selected_base_record_is_not_duplicated() -> None:
    source = "Надо позвонить Сергею"
    model = model_with(
        candidate(
            CandidateType.TASK,
            source,
            CandidateModality.COMMITMENT,
        )
    )

    outcome = await ClassifySource(model).execute(
        ClassificationSource(text=source, base_type=CandidateType.TASK)
    )

    assert outcome.candidates[0].disposition is CandidateDisposition.ALREADY_CAPTURED
    assert (
        outcome.candidates[0].validation_code
        is CandidateValidationCode.ALREADY_CAPTURED
    )


@pytest.mark.asyncio
async def test_default_note_fragments_do_not_create_more_notes() -> None:
    model = model_with(
        candidate(
            CandidateType.NOTE,
            "PostgreSQL поддерживает FTS",
            CandidateModality.OBSERVATION,
        )
    )

    outcome = await ClassifySource(model).execute(
        ClassificationSource(
            text="Заметка. PostgreSQL поддерживает FTS.",
            base_type=CandidateType.NOTE,
        )
    )

    assert outcome.candidates[0].disposition is CandidateDisposition.NEEDS_REVIEW
    assert (
        outcome.candidates[0].validation_code
        is CandidateValidationCode.BASE_NOTE_FRAGMENT
    )


@pytest.mark.asyncio
async def test_invalid_candidate_does_not_block_valid_sibling() -> None:
    model = model_with(
        candidate(
            CandidateType.TASK,
            "Модель переписала исходную фразу",
            CandidateModality.COMMITMENT,
        ),
        candidate(
            CandidateType.QUESTION,
            "Использовать Qdrant?",
            CandidateModality.QUESTION,
        ),
    )

    outcome = await ClassifySource(model).execute(
        ClassificationSource(
            text="Использовать Qdrant?",
            base_type=CandidateType.NOTE,
        )
    )

    assert [item.disposition for item in outcome.candidates] == [
        CandidateDisposition.NEEDS_REVIEW,
        CandidateDisposition.MATERIALIZE,
    ]
    assert [item.validation_code for item in outcome.candidates] == [
        CandidateValidationCode.QUOTE_NOT_FOUND,
        CandidateValidationCode.VALID,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft", "expected_code"),
    [
        (
            candidate(
                CandidateType.IDEA,
                "Можно попробовать GraphRAG",
                CandidateModality.SUGGESTION,
                0.89,
            ),
            CandidateValidationCode.LOW_CONFIDENCE,
        ),
        (
            candidate(
                CandidateType.TASK,
                "Можно попробовать GraphRAG",
                CandidateModality.OBSERVATION,
            ),
            CandidateValidationCode.TYPE_MODALITY_MISMATCH,
        ),
        (
            candidate(
                CandidateType.IDEA,
                "Можно попробовать GraphRAG",
                CandidateModality.SUGGESTION,
                nan,
            ),
            CandidateValidationCode.INVALID_CONFIDENCE,
        ),
    ],
)
async def test_unsafe_candidate_is_kept_for_review_with_closed_reason(
    draft: ClassificationCandidateDraft,
    expected_code: CandidateValidationCode,
) -> None:
    outcome = await ClassifySource(model_with(draft)).execute(
        ClassificationSource(
            text="Можно попробовать GraphRAG",
            base_type=CandidateType.NOTE,
        )
    )

    result = outcome.candidates[0]
    assert result.disposition is CandidateDisposition.NEEDS_REVIEW
    assert result.validation_code is expected_code
    if expected_code is CandidateValidationCode.INVALID_CONFIDENCE:
        assert result.confidence is None


@pytest.mark.asyncio
async def test_duplicate_candidates_and_candidates_after_eight_are_discarded() -> None:
    drafts = tuple(
        candidate(
            CandidateType.TASK,
            f"Задача {index}",
            CandidateModality.COMMITMENT,
        )
        for index in range(9)
    )
    model = model_with(drafts[0], drafts[0], *drafts[1:])
    model.draft = ClassificationDraft(
        model_name=model.draft.model_name,
        prompt_version=model.draft.prompt_version,
        schema_version=model.draft.schema_version,
        candidates=model.draft.candidates,
        discarded_candidate_count=2,
    )

    outcome = await ClassifySource(model).execute(
        ClassificationSource(
            text=". ".join(item.source_quote for item in drafts),
            base_type=CandidateType.NOTE,
        )
    )

    assert len(outcome.candidates) == 8
    assert outcome.discarded_candidate_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN PRIVATE KEY-----\nprivate-material",
        "api_key = sk-examplevalue12345678901234567890",
        "token: 123456789:abcdefghijklmnopqrstuvwxyzABCDE12345",
        "password=hunter2",
    ],
)
async def test_credentials_skip_model_without_exposing_secret(secret: str) -> None:
    model = model_with()

    outcome = await ClassifySource(model).execute(
        ClassificationSource(text=secret, base_type=CandidateType.NOTE)
    )

    assert outcome.skipped_reason == "credential_detected"
    assert outcome.candidates == ()
    assert model.requests == []
    assert secret not in repr(outcome)


@pytest.mark.asyncio
async def test_natural_token_words_do_not_trigger_credential_scanner() -> None:
    model = model_with()

    outcome = await ClassifySource(model).execute(
        ClassificationSource(
            text="Надо обсудить token budget модели",
            base_type=CandidateType.NOTE,
        )
    )

    assert outcome.skipped_reason is None
    assert len(model.requests) == 1

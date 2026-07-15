from second_brain.slices.memory.application.contracts import (
    ReasoningModel,
    ReasoningRequest,
)
from second_brain.slices.memory.application.prompt_builder import (
    LabelProvenance,
    build_reasoning_prompt,
)
from second_brain.slices.memory.application.structured_output import (
    validate_source_labels,
)
from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    EvidenceSnippet,
    MemoryAnswer,
)

# Insufficient answers carry a short honest body: the display leads with the
# evidence badge (render_answer ignores this text for the insufficient level),
# but persistence requires a non-empty answer_text, so it must never be blank.
INSUFFICIENT_ANSWER_TEXT = "В памяти нет ответа."


class AnswerMemoryQuestion:
    def __init__(self, reasoner: ReasoningModel) -> None:
        self._reasoner = reasoner

    async def execute(
        self, question: str, snippets: tuple[EvidenceSnippet, ...]
    ) -> MemoryAnswer:
        if not snippets:
            return MemoryAnswer(
                evidence_level=EvidenceLevel.INSUFFICIENT,
                answer_text=INSUFFICIENT_ANSWER_TEXT,
                sources=(),
                model_name=None,
                prompt_version=None,
                schema_version=None,
            )

        labelled, label_map = build_reasoning_prompt(question, snippets)
        request = ReasoningRequest(question=question, snippets=labelled)
        draft = await self._reasoner.reason(request)
        labels = validate_source_labels(
            draft.evidence_level, draft.source_labels, tuple(label_map)
        )
        sources = tuple(_source(label, label_map[label]) for label in labels)
        return MemoryAnswer(
            evidence_level=draft.evidence_level,
            answer_text=draft.answer,
            sources=sources,
            model_name=draft.model_name,
            prompt_version=draft.prompt_version,
            schema_version=draft.schema_version,
        )


def _source(label: str, provenance: LabelProvenance) -> AnswerSource:
    record_kind, record_id, source_capture_event_id, created_at = provenance
    return AnswerSource(
        label=label,
        record_kind=record_kind,
        record_id=record_id,
        source_capture_event_id=source_capture_event_id,
        created_at=created_at,
    )

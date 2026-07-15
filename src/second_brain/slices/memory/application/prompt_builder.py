from datetime import datetime
from uuid import UUID

from second_brain.slices.memory.application.contracts import LabelledSnippet
from second_brain.slices.memory.domain.entities import EvidenceSnippet, MemoryRecordKind

LabelProvenance = tuple[MemoryRecordKind, UUID, UUID, datetime]


def build_reasoning_prompt(
    question: str, snippets: tuple[EvidenceSnippet, ...]
) -> tuple[tuple[LabelledSnippet, ...], dict[str, LabelProvenance]]:
    # Carry each snippet's own snapshot label through unchanged. Re-numbering here
    # would let the model reason about text under one label while provenance and
    # answer_sources point at a different snapshot row. The model receives only
    # {label, text}; the provenance map stays application-side and is never sent.
    labelled: list[LabelledSnippet] = []
    label_map: dict[str, LabelProvenance] = {}
    for snippet in snippets:
        label = snippet.label
        labelled.append(LabelledSnippet(label=label, text=snippet.text))
        label_map[label] = (
            snippet.record_kind,
            snippet.record_id,
            snippet.source_capture_event_id,
            snippet.created_at,
        )
    return tuple(labelled), label_map

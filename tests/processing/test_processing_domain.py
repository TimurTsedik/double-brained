from itertools import product

from second_brain.slices.processing.application.contracts import TranscriptionDraft
from second_brain.slices.processing.domain.entities import (
    ProcessingStepStatus,
    ProcessingStepType,
    TranscriptionOutputType,
    TranscriptSegment,
    TranscriptWord,
    overall_status,
)


def test_processing_status_has_stable_machine_order() -> None:
    assert [(status.name, status.value) for status in ProcessingStepStatus] == [
        ("FAILED", 0),
        ("NEEDS_REVIEW", 1),
        ("RUNNING", 2),
        ("PENDING", 3),
        ("SUCCEEDED", 4),
        ("SKIPPED", 5),
    ]


def test_overall_status_is_lowest_numeric_step_status() -> None:
    assert overall_status(()) is ProcessingStepStatus.PENDING
    for left, right in product(ProcessingStepStatus, repeat=2):
        assert overall_status((left, right)) is ProcessingStepStatus(
            min(left.value, right.value)
        )


def test_processing_enums_are_fixed() -> None:
    assert [step.value for step in ProcessingStepType] == [
        "audio_download",
        "transcription",
        "classification",
    ]
    assert [output.value for output in TranscriptionOutputType] == [
        "note",
        "task",
        "idea",
        "decision",
        "question",
    ]


def test_transcript_content_is_absent_from_representations() -> None:
    word = TranscriptWord(0.5, 0.8, "secret word")
    segment = TranscriptSegment(0.5, 1.2, "secret segment", (word,))
    draft = TranscriptionDraft(
        text="secret transcript",
        language="ru",
        language_probability=0.99,
        model_name="local-model",
        segments=(segment,),
    )

    assert "secret word" not in repr(word)
    assert "secret segment" not in repr(segment)
    assert "secret transcript" not in repr(draft)

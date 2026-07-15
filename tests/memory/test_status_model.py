from datetime import UTC, datetime
from itertools import product
from uuid import uuid4

from second_brain.slices.memory.domain.entities import (
    AnswerSource,
    EvidenceLevel,
    EvidenceSnippet,
    MemoryAnswer,
    MemoryAnswerRun,
    MemoryAnswerStep,
    MemoryQuestion,
    MemoryReasoningState,
    MemoryRecordKind,
    MemoryRunClaim,
    MemoryRunStatus,
    MemoryStepType,
    overall_status,
)

_STATUS_NAMES = (
    "FAILED",
    "NEEDS_REVIEW",
    "RUNNING",
    "PENDING",
    "SUCCEEDED",
    "SKIPPED",
)


def test_memory_run_status_has_stable_machine_order() -> None:
    assert [(status.name, status.value) for status in MemoryRunStatus] == [
        ("FAILED", 0),
        ("NEEDS_REVIEW", 1),
        ("RUNNING", 2),
        ("PENDING", 3),
        ("SUCCEEDED", 4),
        ("SKIPPED", 5),
    ]


def test_memory_run_status_pins_processing_step_status() -> None:
    # memory owns its OWN status enum; this test pins numeric equivalence so a
    # drift in ProcessingStepStatus is caught here (the enum lives only in the
    # test, never imported into memory production code).
    from second_brain.slices.processing.domain.entities import ProcessingStepStatus

    assert [(status.name, status.value) for status in MemoryRunStatus] == [
        (status.name, status.value) for status in ProcessingStepStatus
    ]
    for name in _STATUS_NAMES:
        assert int(MemoryRunStatus[name]) == int(ProcessingStepStatus[name])


def test_overall_status_is_lowest_numeric_step_status() -> None:
    assert overall_status(()) is MemoryRunStatus.PENDING
    for left, right in product(MemoryRunStatus, repeat=2):
        assert overall_status((left, right)) is MemoryRunStatus(
            min(left.value, right.value)
        )


def test_memory_step_type_is_three_ordered_steps() -> None:
    assert [step.value for step in MemoryStepType] == [
        "retrieval",
        "reasoning",
        "delivery",
    ]


def test_evidence_level_has_full_set() -> None:
    assert [level.value for level in EvidenceLevel] == [
        "direct",
        "reconstructed",
        "hypothesis",
        "insufficient",
    ]


def test_memory_record_kind_is_fixed() -> None:
    assert [kind.value for kind in MemoryRecordKind] == [
        "note",
        "task",
        "idea",
        "decision",
        "question",
    ]


def test_content_and_identifiers_are_absent_from_representations() -> None:
    question_id = uuid4()
    run_id = uuid4()
    step_id = uuid4()
    user_space_id = uuid4()
    record_id = uuid4()
    source_capture_event_id = uuid4()
    project_id = uuid4()
    identifiers = (
        question_id,
        run_id,
        step_id,
        user_space_id,
        record_id,
        source_capture_event_id,
        project_id,
    )
    created_at = datetime(2026, 7, 15, tzinfo=UTC)

    question = MemoryQuestion(
        id=question_id,
        user_space_id=user_space_id,
        bot_id=999111,
        telegram_update_id=777222,
        question_text="secret question about pricing",
        current_project_id=project_id,
        created_at=created_at,
        trace_id="trace-secret-question",
    )
    source = AnswerSource(
        label="S1",
        record_kind=MemoryRecordKind.NOTE,
        record_id=record_id,
        source_capture_event_id=source_capture_event_id,
        created_at=created_at,
    )
    answer = MemoryAnswer(
        evidence_level=EvidenceLevel.DIRECT,
        answer_text="secret answer text",
        sources=(source,),
        model_name="nvidia/nemotron",
        prompt_version="grounded-answer-v1",
        schema_version="grounded-answer-v1",
    )
    snippet = EvidenceSnippet(
        label="S1",
        record_kind=MemoryRecordKind.NOTE,
        record_id=record_id,
        source_capture_event_id=source_capture_event_id,
        created_at=created_at,
        text="secret snippet body",
    )
    step = MemoryAnswerStep(
        id=step_id,
        step_type=MemoryStepType.REASONING,
        status=MemoryRunStatus.PENDING,
        attempt_count=0,
        next_attempt_at=None,
        lease_expires_at=None,
        safe_error_code=None,
        started_at=None,
        completed_at=None,
    )
    run = MemoryAnswerRun(
        id=run_id,
        user_space_id=user_space_id,
        question_id=question_id,
        steps=(step,),
        created_at=created_at,
        trace_id="trace-secret-run",
    )
    claim = MemoryRunClaim(
        step_id=step_id,
        run_id=run_id,
        question_id=question_id,
        step_type=MemoryStepType.REASONING,
        attempt_count=1,
        lease_expires_at=created_at,
        trace_id="trace-secret-claim",
    )
    reasoning_state = MemoryReasoningState(
        status=MemoryRunStatus.SUCCEEDED,
        has_answer=True,
    )

    representations = (
        repr(question),
        repr(source),
        repr(answer),
        repr(snippet),
        repr(step),
        repr(run),
        repr(claim),
        repr(reasoning_state),
    )
    secrets = (
        "secret question about pricing",
        "secret answer text",
        "secret snippet body",
        "trace-secret-question",
        "trace-secret-run",
        "trace-secret-claim",
        "999111",
        "777222",
    )
    for text in representations:
        for secret in secrets:
            assert secret not in text
        for identifier in identifiers:
            assert str(identifier) not in text


def test_run_overall_status_is_min_over_steps() -> None:
    def _step(step_type: MemoryStepType, status: MemoryRunStatus) -> MemoryAnswerStep:
        return MemoryAnswerStep(
            id=uuid4(),
            step_type=step_type,
            status=status,
            attempt_count=0,
            next_attempt_at=None,
            lease_expires_at=None,
            safe_error_code=None,
            started_at=None,
            completed_at=None,
        )

    run = MemoryAnswerRun(
        id=uuid4(),
        user_space_id=uuid4(),
        question_id=uuid4(),
        steps=(
            _step(MemoryStepType.RETRIEVAL, MemoryRunStatus.SUCCEEDED),
            _step(MemoryStepType.REASONING, MemoryRunStatus.RUNNING),
            _step(MemoryStepType.DELIVERY, MemoryRunStatus.PENDING),
        ),
        created_at=datetime(2026, 7, 15, tzinfo=UTC),
        trace_id="trace",
    )
    assert run.overall_status is MemoryRunStatus.RUNNING

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from second_brain.slices.processing.adapters.transcription import faster_whisper
from second_brain.slices.processing.adapters.transcription.faster_whisper import (
    FasterWhisperTranscriptionModel,
    TranscriptionFailure,
)
from second_brain.slices.processing.application.contracts import (
    TranscribeVoiceCommand,
)

MODEL = "small"
LOCAL_PATH = "/private/user-space/capture/original.ogg"


class FakeWord:
    def __init__(self, start: float, end: float, word: str) -> None:
        self.start = start
        self.end = end
        self.word = word


class FakeSegment:
    def __init__(self, start: float, end: float, text: str, words: Any) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = words


class FakeInfo:
    def __init__(self, language: str, language_probability: float | None) -> None:
        self.language = language
        self.language_probability = language_probability


class FakeWhisperModel:
    def __init__(
        self,
        segments_factory: Callable[[], Iterator[object]],
        info: object,
        error: Exception | None = None,
    ) -> None:
        self._segments_factory = segments_factory
        self._info = info
        self._error = error
        self.calls: list[tuple[str, bool]] = []

    def transcribe(
        self, audio: str, *, word_timestamps: bool
    ) -> tuple[Iterator[object], object]:
        self.calls.append((audio, word_timestamps))
        if self._error is not None:
            raise self._error
        return self._segments_factory(), self._info


class FakeFasterWhisperModule:
    def __init__(self, model: FakeWhisperModel) -> None:
        self._model = model
        self.constructions: list[tuple[str, str, str]] = []

    def WhisperModel(  # noqa: N802 - mirrors the faster-whisper class name
        self, model_name: str, *, device: str, compute_type: str
    ) -> FakeWhisperModel:
        self.constructions.append((model_name, device, compute_type))
        return self._model


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    segments: list[object],
    info: object,
    error: Exception | None = None,
) -> tuple[FakeFasterWhisperModule, FakeWhisperModel, list[object], list[str]]:
    consumed: list[object] = []

    def segments_factory() -> Iterator[object]:
        # A real generator: nothing is produced (and text stays empty) unless the
        # adapter materializes it inside the worker thread.
        for segment in segments:
            consumed.append(segment)
            yield segment

    whisper = FakeWhisperModel(segments_factory, info, error)
    module = FakeFasterWhisperModule(whisper)
    imported: list[str] = []

    def import_module(name: str) -> Any:
        imported.append(name)
        return module

    monkeypatch.setattr(faster_whisper.importlib, "import_module", import_module)
    return module, whisper, consumed, imported


def valid_segments() -> list[object]:
    return [
        FakeSegment(0.0, 1.5, " Привет, ", [FakeWord(0.0, 0.7, " Привет,")]),
        FakeSegment(1.6, 2.4, " мир. ", [FakeWord(1.6, 2.4, " мир.")]),
    ]


@pytest.mark.asyncio
async def test_lazy_generator_is_materialized_and_normalized_with_word_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, whisper, consumed, imported = _install(
        monkeypatch,
        segments=valid_segments(),
        info=FakeInfo("ru", 0.98),
    )
    model = FasterWhisperTranscriptionModel(MODEL)
    assert imported == []
    assert module.constructions == []

    result = await model.transcribe(TranscribeVoiceCommand(local_path=LOCAL_PATH))

    assert imported == ["faster_whisper"]
    assert module.constructions == [(MODEL, "cpu", "int8")]
    assert whisper.calls == [(LOCAL_PATH, True)]
    assert len(consumed) == 2  # the lazy generator was actually consumed
    assert result.text == "Привет, мир."
    assert result.language == "ru"
    assert result.language_probability == 0.98
    assert result.model_name == MODEL
    assert len(result.segments) == 2
    assert result.segments[0].text == "Привет,"
    assert [word.text for word in result.segments[0].words] == ["Привет,"]
    assert result.segments[1].words[0].start == 1.6


@pytest.mark.asyncio
async def test_segment_without_words_normalizes_to_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        segments=[FakeSegment(0.0, 1.0, "Привет", None)],
        info=FakeInfo("ru", None),
    )

    result = await FasterWhisperTranscriptionModel(MODEL).transcribe(
        TranscribeVoiceCommand(local_path=LOCAL_PATH)
    )

    assert result.language_probability is None
    assert result.segments[0].words == ()


@pytest.mark.asyncio
async def test_blank_transcript_has_its_own_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        segments=[FakeSegment(0.0, 1.0, "  \n ", None)],
        info=FakeInfo("ru", 0.9),
    )

    with pytest.raises(TranscriptionFailure) as failure:
        await FasterWhisperTranscriptionModel(MODEL).transcribe(
            TranscribeVoiceCommand(local_path=LOCAL_PATH)
        )

    assert failure.value.safe_error_code == "empty_transcript"


@pytest.mark.asyncio
async def test_provider_exception_becomes_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        segments=[],
        info=FakeInfo("ru", 0.9),
        error=RuntimeError("provider leaked transcript and /private/path"),
    )

    with pytest.raises(TranscriptionFailure) as failure:
        await FasterWhisperTranscriptionModel(MODEL).transcribe(
            TranscribeVoiceCommand(local_path=LOCAL_PATH)
        )

    assert failure.value.safe_error_code == "transcription_failed"
    assert "provider leaked" not in str(failure.value)
    assert "/private/path" not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.asyncio
async def test_transcript_text_is_hidden_from_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        segments=valid_segments(),
        info=FakeInfo("ru", 0.98),
    )

    result = await FasterWhisperTranscriptionModel(MODEL).transcribe(
        TranscribeVoiceCommand(local_path=LOCAL_PATH)
    )

    assert "Привет" not in repr(result)
    assert "Привет" not in repr(result.segments[0])


@pytest.mark.asyncio
async def test_model_is_loaded_lazily_once_and_reused_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, whisper, _consumed, imported = _install(
        monkeypatch,
        segments=valid_segments(),
        info=FakeInfo("ru", 0.98),
    )
    model = FasterWhisperTranscriptionModel(MODEL)
    assert module.constructions == []  # not loaded in the constructor

    await model.transcribe(TranscribeVoiceCommand(local_path=LOCAL_PATH))
    await model.transcribe(TranscribeVoiceCommand(local_path=LOCAL_PATH))

    assert module.constructions == [(MODEL, "cpu", "int8")]  # loaded exactly once
    assert imported == ["faster_whisper"]
    assert whisper.calls == [(LOCAL_PATH, True), (LOCAL_PATH, True)]


def test_runtime_preflight_requires_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FasterWhisperTranscriptionModel(MODEL)
    monkeypatch.setattr(faster_whisper.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="FFmpeg"):
        model.ensure_runtime()

    monkeypatch.setattr(
        faster_whisper.shutil, "which", lambda _name: "/usr/local/bin/ffmpeg"
    )
    model.ensure_runtime()

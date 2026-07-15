import asyncio
import importlib
import shutil
from collections.abc import Iterable
from typing import Protocol, cast

from second_brain.slices.processing.application.contracts import (
    TranscribeVoiceCommand,
    TranscriptionDraft,
)
from second_brain.slices.processing.domain.entities import (
    TranscriptSegment,
    TranscriptWord,
)


class TranscriptionFailure(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class _WhisperModel(Protocol):
    def transcribe(
        self, audio: str, *, word_timestamps: bool
    ) -> tuple[Iterable[object], object]: ...


class _FasterWhisperModule(Protocol):
    def WhisperModel(  # noqa: N802 - mirrors the faster-whisper class name
        self, model_name: str, *, device: str, compute_type: str
    ) -> _WhisperModel: ...


class FasterWhisperTranscriptionModel:
    def __init__(self, model_name: str) -> None:
        if not model_name:
            raise ValueError("Whisper model name must not be empty")
        self._model_name = model_name
        # Loaded lazily on the first transcribe and reused across calls; kept out
        # of repr so a large model handle never dumps into a repr/log.
        self._model: _WhisperModel | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model_name={self._model_name!r})"

    def ensure_runtime(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("FFmpeg is required for local transcription")

    async def transcribe(self, command: TranscribeVoiceCommand) -> TranscriptionDraft:
        try:
            return await asyncio.to_thread(self._transcribe_sync, command.local_path)
        except TranscriptionFailure:
            raise
        except Exception:
            raise TranscriptionFailure("transcription_failed") from None

    def _transcribe_sync(self, local_path: str) -> TranscriptionDraft:
        model = self._load_model()
        segments, info = model.transcribe(local_path, word_timestamps=True)
        # faster-whisper yields segments lazily and does the actual work while the
        # generator is consumed, so materialize it inside the thread; otherwise the
        # transcript comes back empty.
        materialized = list(segments)
        return _normalize_result(materialized, info, self._model_name)

    def _load_model(self) -> _WhisperModel:
        if self._model is None:
            module = cast(
                _FasterWhisperModule,
                importlib.import_module("faster_whisper"),
            )
            self._model = module.WhisperModel(
                self._model_name, device="cpu", compute_type="int8"
            )
        return self._model


def _normalize_result(
    raw_segments: list[object], info: object, model_name: str
) -> TranscriptionDraft:
    try:
        segments = _segments(raw_segments)
        text = " ".join(segment.text for segment in segments)
        if not text:
            raise TranscriptionFailure("empty_transcript")
        language = _required_text(getattr(info, "language", None))
        if not language:
            raise ValueError
        probability = _optional_probability(getattr(info, "language_probability", None))
    except TranscriptionFailure:
        raise
    except (TypeError, ValueError, KeyError, AttributeError):
        raise TranscriptionFailure("invalid_transcript") from None
    return TranscriptionDraft(
        text=text,
        language=language,
        language_probability=probability,
        model_name=model_name,
        segments=segments,
    )


def _segments(raw_segments: list[object]) -> tuple[TranscriptSegment, ...]:
    normalized: list[TranscriptSegment] = []
    for raw_segment in raw_segments:
        start, end = _time_range(raw_segment)
        words = _words(getattr(raw_segment, "words", None))
        text = _required_text(getattr(raw_segment, "text", None))
        if not text:
            continue
        normalized.append(TranscriptSegment(start, end, text, words))
    return tuple(normalized)


def _words(value: object) -> tuple[TranscriptWord, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError
    normalized: list[TranscriptWord] = []
    for raw_word in value:
        start, end = _time_range(raw_word)
        text = _required_text(getattr(raw_word, "word", None))
        if not text:
            raise ValueError
        normalized.append(TranscriptWord(start, end, text))
    return tuple(normalized)


def _time_range(value: object) -> tuple[float, float]:
    start = _number(getattr(value, "start", None))
    end = _number(getattr(value, "end", None))
    if start < 0 or end < start:
        raise ValueError
    return start, end


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    return float(value)


def _required_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return " ".join(value.split())


def _optional_probability(value: object) -> float | None:
    if value is None:
        return None
    probability = _number(value)
    if not 0 <= probability <= 1:
        raise ValueError
    return probability

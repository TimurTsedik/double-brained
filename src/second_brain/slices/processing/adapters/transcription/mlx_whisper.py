import asyncio
import importlib
import shutil
from collections.abc import Mapping
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


class _MlxWhisperModule(Protocol):
    def transcribe(
        self,
        audio: str,
        *,
        path_or_hf_repo: str,
        word_timestamps: bool,
    ) -> Mapping[str, object]: ...


class MlxWhisperTranscriptionModel:
    def __init__(self, model_name: str) -> None:
        if not model_name:
            raise ValueError("MLX Whisper model name must not be empty")
        self._model_name = model_name

    def ensure_runtime(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("FFmpeg is required for local transcription")

    async def transcribe(self, command: TranscribeVoiceCommand) -> TranscriptionDraft:
        try:
            raw_result = await asyncio.to_thread(
                _transcribe_sync, command.local_path, self._model_name
            )
        except Exception:
            raise TranscriptionFailure("transcription_failed") from None
        return _normalize_result(raw_result, self._model_name)


def _transcribe_sync(local_path: str, model_name: str) -> Mapping[str, object]:
    module = cast(
        _MlxWhisperModule,
        importlib.import_module("mlx_whisper"),
    )
    return module.transcribe(
        local_path,
        path_or_hf_repo=model_name,
        word_timestamps=True,
    )


def _normalize_result(
    raw_result: Mapping[str, object], model_name: str
) -> TranscriptionDraft:
    try:
        text = _required_text(raw_result.get("text"))
        if not text:
            raise TranscriptionFailure("empty_transcript")
        language = _required_text(raw_result.get("language"))
        if not language:
            raise ValueError
        probability = _optional_probability(raw_result.get("language_probability"))
        segments = _segments(raw_result.get("segments"))
    except TranscriptionFailure:
        raise
    except (TypeError, ValueError, KeyError):
        raise TranscriptionFailure("invalid_transcript") from None
    return TranscriptionDraft(
        text=text,
        language=language,
        language_probability=probability,
        model_name=model_name,
        segments=segments,
    )


def _segments(value: object) -> tuple[TranscriptSegment, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError
    normalized: list[TranscriptSegment] = []
    for raw_segment in value:
        if not isinstance(raw_segment, Mapping):
            raise ValueError
        start, end = _time_range(raw_segment)
        text = _required_text(raw_segment.get("text"))
        if not text:
            if not isinstance(raw_segment.get("words"), list):
                raise ValueError
            continue
        words = _words(raw_segment.get("words"))
        normalized.append(TranscriptSegment(start, end, text, words))
    if not normalized:
        raise ValueError
    return tuple(normalized)


def _words(value: object) -> tuple[TranscriptWord, ...]:
    if not isinstance(value, list):
        raise ValueError
    normalized: list[TranscriptWord] = []
    for raw_word in value:
        if not isinstance(raw_word, Mapping):
            raise ValueError
        start, end = _time_range(raw_word)
        text = _required_text(raw_word.get("word"))
        if not text:
            raise ValueError
        normalized.append(TranscriptWord(start, end, text))
    return tuple(normalized)


def _time_range(value: Mapping[object, object]) -> tuple[float, float]:
    start = _number(value.get("start"))
    end = _number(value.get("end"))
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

from collections.abc import Mapping
from typing import Any

import pytest

from second_brain.slices.processing.adapters.transcription import mlx_whisper
from second_brain.slices.processing.adapters.transcription.mlx_whisper import (
    MlxWhisperTranscriptionModel,
    TranscriptionFailure,
)
from second_brain.slices.processing.application.contracts import (
    TranscribeVoiceCommand,
)

MODEL = "mlx-community/whisper-large-v3-turbo"
LOCAL_PATH = "/private/user-space/capture/original.ogg"


class FakeMlxWhisper:
    def __init__(
        self,
        result: Mapping[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str, bool]] = []

    def transcribe(
        self,
        audio: str,
        *,
        path_or_hf_repo: str,
        word_timestamps: bool,
    ) -> Mapping[str, object]:
        self.calls.append((audio, path_or_hf_repo, word_timestamps))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def valid_result() -> dict[str, object]:
    return {
        "text": "  Привет,\n   мир.  ",
        "language": "ru",
        "language_probability": 0.98,
        "segments": [
            {
                "start": 0.0,
                "end": 1.5,
                "text": " Привет, мир. ",
                "words": [
                    {"start": 0.0, "end": 0.7, "word": " Привет,"},
                    {"start": 0.8, "end": 1.5, "word": " мир."},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_mlx_is_loaded_lazily_and_normalized_with_word_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeMlxWhisper(valid_result())
    imported: list[str] = []

    def import_module(name: str) -> Any:
        imported.append(name)
        return provider

    monkeypatch.setattr(mlx_whisper.importlib, "import_module", import_module)
    model = MlxWhisperTranscriptionModel(MODEL)
    assert imported == []

    result = await model.transcribe(TranscribeVoiceCommand(local_path=LOCAL_PATH))

    assert imported == ["mlx_whisper"]
    assert provider.calls == [(LOCAL_PATH, MODEL, True)]
    assert result.text == "Привет, мир."
    assert result.language == "ru"
    assert result.language_probability == 0.98
    assert result.model_name == MODEL
    assert len(result.segments) == 1
    assert result.segments[0].text == "Привет, мир."
    assert [word.text for word in result.segments[0].words] == ["Привет,", "мир."]
    assert result.segments[0].words[1].start == 0.8
    assert LOCAL_PATH not in repr(TranscribeVoiceCommand(local_path=LOCAL_PATH))
    assert "Привет" not in repr(result)
    assert "Привет" not in repr(result.segments[0])


@pytest.mark.asyncio
async def test_provider_exception_becomes_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeMlxWhisper(
        error=RuntimeError("provider leaked transcript and /private/path")
    )
    monkeypatch.setattr(mlx_whisper.importlib, "import_module", lambda _name: provider)

    with pytest.raises(TranscriptionFailure) as failure:
        await MlxWhisperTranscriptionModel(MODEL).transcribe(
            TranscribeVoiceCommand(local_path=LOCAL_PATH)
        )

    assert failure.value.safe_error_code == "transcription_failed"
    assert "provider leaked" not in str(failure.value)
    assert "/private/path" not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.asyncio
async def test_empty_transcript_has_its_own_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = valid_result()
    result["text"] = " \n "
    provider = FakeMlxWhisper(result)
    monkeypatch.setattr(mlx_whisper.importlib, "import_module", lambda _name: provider)

    with pytest.raises(TranscriptionFailure) as failure:
        await MlxWhisperTranscriptionModel(MODEL).transcribe(
            TranscribeVoiceCommand(local_path=LOCAL_PATH)
        )

    assert failure.value.safe_error_code == "empty_transcript"


@pytest.mark.asyncio
async def test_provider_empty_segments_are_ignored_without_rejecting_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = valid_result()
    result.pop("language_probability")
    result["segments"] = [
        {"start": 0.0, "end": 0.0, "text": "", "words": []},
        *result["segments"],
    ]
    provider = FakeMlxWhisper(result)
    monkeypatch.setattr(mlx_whisper.importlib, "import_module", lambda _name: provider)

    transcript = await MlxWhisperTranscriptionModel(MODEL).transcribe(
        TranscribeVoiceCommand(local_path=LOCAL_PATH)
    )

    assert transcript.language_probability is None
    assert len(transcript.segments) == 1
    assert transcript.segments[0].text == "Привет, мир."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("language"),
        lambda value: value.update(language_probability=1.5),
        lambda value: value.update(segments="not-a-list"),
        lambda value: value["segments"][0].update(start="bad"),
        lambda value: value["segments"][0].update(end=-1),
        lambda value: value["segments"][0].update(words="not-a-list"),
        lambda value: value["segments"][0]["words"][0].pop("word"),
        lambda value: value["segments"][0]["words"][0].update(end=-1),
    ],
)
async def test_malformed_provider_output_is_rejected_safely(
    monkeypatch: pytest.MonkeyPatch, mutate: Any
) -> None:
    result = valid_result()
    mutate(result)
    provider = FakeMlxWhisper(result)
    monkeypatch.setattr(mlx_whisper.importlib, "import_module", lambda _name: provider)

    with pytest.raises(TranscriptionFailure) as failure:
        await MlxWhisperTranscriptionModel(MODEL).transcribe(
            TranscribeVoiceCommand(local_path=LOCAL_PATH)
        )

    assert failure.value.safe_error_code == "invalid_transcript"
    assert LOCAL_PATH not in str(failure.value)


def test_runtime_preflight_requires_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    model = MlxWhisperTranscriptionModel(MODEL)
    monkeypatch.setattr(mlx_whisper.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="FFmpeg"):
        model.ensure_runtime()

    monkeypatch.setattr(
        mlx_whisper.shutil, "which", lambda _name: "/usr/local/bin/ffmpeg"
    )
    model.ensure_runtime()

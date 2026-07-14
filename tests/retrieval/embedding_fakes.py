import hashlib
import re

import pytest

from second_brain.slices.retrieval.adapters.embedding import e5
from second_brain.slices.retrieval.domain.entities import IndexedChunk

_FAKE_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…]) ")


class FakeEmbeddingModel:
    """Deterministic EmbeddingModel port fake: one chunk per sentence,
    checksum-derived unit vectors, no real model download."""

    def __init__(
        self,
        *,
        dimensions: int = 768,
        error: Exception | None = None,
    ) -> None:
        self.document_calls: list[str] = []
        self.query_calls: list[str] = []
        self._dimensions = dimensions
        self._error = error

    async def embed_document(self, text: str) -> tuple[IndexedChunk, ...]:
        if self._error is not None:
            raise self._error
        self.document_calls.append(text)
        normalized = " ".join(text.split())
        pieces = [piece for piece in _FAKE_SENTENCE_BOUNDARY.split(normalized) if piece]
        return tuple(
            IndexedChunk(
                chunk_number=chunk_number,
                content_sha256=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                text=piece,
                embedding=self._unit_vector(piece),
            )
            for chunk_number, piece in enumerate(pieces)
        )

    async def embed_query(self, text: str) -> tuple[float, ...]:
        if self._error is not None:
            raise self._error
        self.query_calls.append(text)
        return self._unit_vector(f"query: {text}")

    def _unit_vector(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self._dimensions
        vector[sum(text.encode("utf-8")) % self._dimensions] = 1.0
        return tuple(vector)


class FakeWordTokenizer:
    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._words: list[str] = []

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        assert add_special_tokens is False
        token_ids: list[int] = []
        for word in text.split():
            if word not in self._ids:
                self._ids[word] = len(self._words)
                self._words.append(word)
            token_ids.append(self._ids[word])
        return token_ids

    def decode(self, token_ids: list[int], skip_special_tokens: bool = True) -> str:
        return " ".join(self._words[token_id] for token_id in token_ids)


class FakeSentenceTransformer:
    def __init__(
        self,
        model_name: str,
        *,
        dimensions: int = 768,
        error: Exception | None = None,
    ) -> None:
        self.model_name = model_name
        self.tokenizer = FakeWordTokenizer()
        self.encode_calls: list[tuple[tuple[str, ...], bool]] = []
        self._dimensions = dimensions
        self._error = error

    def encode(
        self,
        sentences: list[str],
        *,
        normalize_embeddings: bool = False,
    ) -> list[list[float]]:
        self.encode_calls.append((tuple(sentences), normalize_embeddings))
        if self._error is not None:
            raise self._error
        return [self._unit_vector(sentence) for sentence in sentences]

    def _unit_vector(self, sentence: str) -> list[float]:
        vector = [0.0] * self._dimensions
        vector[sum(sentence.encode("utf-8")) % self._dimensions] = 1.0
        return vector


class FakeSentenceTransformersModule:
    def __init__(
        self,
        *,
        dimensions: int = 768,
        error: Exception | None = None,
    ) -> None:
        self.created: list[FakeSentenceTransformer] = []
        self._dimensions = dimensions
        self._error = error

    def SentenceTransformer(self, model_name: str) -> FakeSentenceTransformer:
        model = FakeSentenceTransformer(
            model_name,
            dimensions=self._dimensions,
            error=self._error,
        )
        self.created.append(model)
        return model


def install_fake_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
    module: FakeSentenceTransformersModule,
) -> list[str]:
    imported: list[str] = []

    def import_module(name: str) -> FakeSentenceTransformersModule:
        imported.append(name)
        return module

    monkeypatch.setattr(e5.importlib, "import_module", import_module)
    return imported

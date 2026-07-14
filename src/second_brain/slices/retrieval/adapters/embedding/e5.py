import asyncio
import hashlib
import importlib
import re
from collections.abc import Sequence
from typing import Protocol, cast

from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
)
from second_brain.slices.retrieval.domain.entities import IndexedChunk

_MAX_CHUNK_TOKENS = 448
_OVERLAP_TOKENS = 64
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…]) ")


class EmbeddingFailure(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class _Tokenizer(Protocol):
    def encode(self, text: str, add_special_tokens: bool = ...) -> list[int]: ...

    def decode(self, token_ids: list[int], skip_special_tokens: bool = ...) -> str: ...


class _SentenceTransformerModel(Protocol):
    tokenizer: _Tokenizer

    def encode(
        self,
        sentences: list[str],
        *,
        normalize_embeddings: bool = ...,
    ) -> Sequence[Sequence[float]]: ...


class _SentenceTransformersModule(Protocol):
    def SentenceTransformer(  # noqa: N802 - mirrors the library class name
        self, model_name: str
    ) -> _SentenceTransformerModel: ...


class E5EmbeddingModel:
    def __init__(self, model_name: str = EMBEDDING_MODEL_NAME) -> None:
        if not model_name:
            raise ValueError("Embedding model name must not be empty")
        self._model_name = model_name
        self._model: _SentenceTransformerModel | None = None

    async def embed_document(self, text: str) -> tuple[IndexedChunk, ...]:
        normalized = _normalize_text(text)
        if not normalized:
            raise EmbeddingFailure("empty_source")
        try:
            return await asyncio.to_thread(self._embed_document_sync, normalized)
        except EmbeddingFailure:
            raise
        except Exception:
            raise EmbeddingFailure("embedding_failed") from None

    async def embed_query(self, text: str) -> tuple[float, ...]:
        normalized = _normalize_text(text)
        if not normalized:
            raise EmbeddingFailure("empty_source")
        try:
            return await asyncio.to_thread(self._embed_query_sync, normalized)
        except EmbeddingFailure:
            raise
        except Exception:
            raise EmbeddingFailure("embedding_failed") from None

    def _embed_document_sync(self, normalized: str) -> tuple[IndexedChunk, ...]:
        model = self._load_model()
        chunk_texts = _chunk_text(normalized, model.tokenizer)
        vectors = model.encode(
            [f"passage: {chunk_text}" for chunk_text in chunk_texts],
            normalize_embeddings=True,
        )
        if len(vectors) != len(chunk_texts):
            raise EmbeddingFailure("invalid_embedding")
        return tuple(
            IndexedChunk(
                chunk_number=chunk_number,
                content_sha256=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                text=chunk_text,
                embedding=_validated_embedding(vector),
            )
            for chunk_number, (chunk_text, vector) in enumerate(
                zip(chunk_texts, vectors, strict=True)
            )
        )

    def _embed_query_sync(self, normalized: str) -> tuple[float, ...]:
        model = self._load_model()
        vectors = model.encode(
            [f"query: {normalized}"],
            normalize_embeddings=True,
        )
        if len(vectors) != 1:
            raise EmbeddingFailure("invalid_embedding")
        return _validated_embedding(vectors[0])

    def _load_model(self) -> _SentenceTransformerModel:
        if self._model is None:
            module = cast(
                _SentenceTransformersModule,
                importlib.import_module("sentence_transformers"),
            )
            self._model = module.SentenceTransformer(self._model_name)
        return self._model


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _validated_embedding(raw_vector: Sequence[float]) -> tuple[float, ...]:
    embedding = tuple(float(value) for value in raw_vector)
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise EmbeddingFailure("invalid_embedding")
    return embedding


def _chunk_text(normalized: str, tokenizer: _Tokenizer) -> tuple[str, ...]:
    token_chunks: list[list[int]] = []
    current: list[int] = []
    for sentence in _SENTENCE_BOUNDARY.split(normalized):
        sentence_tokens = tokenizer.encode(sentence, add_special_tokens=False)
        if len(current) + len(sentence_tokens) <= _MAX_CHUNK_TOKENS:
            current.extend(sentence_tokens)
            continue
        if current:
            token_chunks.append(current)
            current = current[-_OVERLAP_TOKENS:]
        while len(current) + len(sentence_tokens) > _MAX_CHUNK_TOKENS:
            free_capacity = _MAX_CHUNK_TOKENS - len(current)
            token_chunks.append(current + sentence_tokens[:free_capacity])
            current = token_chunks[-1][-_OVERLAP_TOKENS:]
            sentence_tokens = sentence_tokens[free_capacity:]
        current = current + sentence_tokens
    if current:
        token_chunks.append(current)
    return tuple(
        tokenizer.decode(chunk_tokens, skip_special_tokens=True)
        for chunk_tokens in token_chunks
    )

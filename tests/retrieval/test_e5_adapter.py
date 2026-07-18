import math
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from second_brain.slices.retrieval.adapters.embedding.e5 import (
    E5EmbeddingModel,
    EmbeddingFailure,
)
from second_brain.slices.retrieval.application.contracts import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
    INDEX_VERSION,
    IndexingSource,
)
from second_brain.slices.retrieval.application.indexing import IndexSource
from second_brain.slices.retrieval.domain.entities import (
    IndexedChunk,
    SearchRecordType,
)
from tests.retrieval.embedding_fakes import (
    FakeSentenceTransformersModule,
    install_fake_sentence_transformers,
)


def test_model_and_index_version_are_code_constants() -> None:
    assert EMBEDDING_MODEL_NAME == "intfloat/multilingual-e5-base"
    assert EMBEDDING_DIMENSIONS == 768
    assert INDEX_VERSION == 1


def test_adapter_module_import_does_not_pull_sentence_transformers() -> None:
    assert "sentence_transformers" not in sys.modules


@pytest.mark.asyncio
async def test_documents_and_queries_get_prefixes_normalization_and_768_floats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule()
    imported = install_fake_sentence_transformers(monkeypatch, module)
    model = E5EmbeddingModel()
    assert imported == []

    chunks = await model.embed_document("Хлеб куплен вчера вечером.")
    query_vector = await model.embed_query("  Когда   купили хлеб? ")

    assert imported == ["sentence_transformers"]
    assert [fake.model_name for fake in module.created] == [EMBEDDING_MODEL_NAME]
    fake_model = module.created[0]
    passage_call, query_call = fake_model.encode_calls
    assert passage_call == (
        tuple(f"passage: {chunk.text}" for chunk in chunks),
        True,
    )
    assert query_call == (("query: Когда купили хлеб?",), True)
    assert all(len(chunk.embedding) == 768 for chunk in chunks)
    assert len(query_vector) == 768
    norm = math.sqrt(sum(value * value for value in query_vector))
    assert norm == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_wrong_dimension_from_library_is_rejected_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule(dimensions=5)
    install_fake_sentence_transformers(monkeypatch, module)
    model = E5EmbeddingModel()

    with pytest.raises(EmbeddingFailure) as document_failure:
        await model.embed_document("Хлеб куплен вчера.")
    with pytest.raises(EmbeddingFailure) as query_failure:
        await model.embed_query("Когда купили хлеб?")

    assert document_failure.value.safe_error_code == "invalid_embedding"
    assert query_failure.value.safe_error_code == "invalid_embedding"


@pytest.mark.asyncio
async def test_library_exception_becomes_safe_failure_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule(
        error=RuntimeError("leaked secret text and vector [0.125, 0.25]")
    )
    install_fake_sentence_transformers(monkeypatch, module)

    with pytest.raises(EmbeddingFailure) as failure:
        await E5EmbeddingModel().embed_document("Секретная заметка про хлеб.")

    assert failure.value.safe_error_code == "embedding_failed"
    assert "leaked secret" not in str(failure.value)
    assert "0.125" not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.asyncio
async def test_indexed_chunk_repr_hides_text_and_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule()
    install_fake_sentence_transformers(monkeypatch, module)

    chunks = await E5EmbeddingModel().embed_document("Хлеб куплен вчера.")

    assert "Хлеб" not in repr(chunks[0])
    assert "0.0" not in repr(chunks[0])
    assert "1.0" not in repr(chunks[0])


@pytest.mark.asyncio
async def test_model_is_loaded_once_per_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule()
    imported = install_fake_sentence_transformers(monkeypatch, module)
    model = E5EmbeddingModel()

    await model.embed_document("Хлеб куплен вчера.")
    await model.embed_document("Молоко куплено сегодня.")
    await model.embed_query("Что купили?")

    assert imported == ["sentence_transformers"]
    assert len(module.created) == 1


class FakeEmbeddingPort:
    def __init__(self, chunks: tuple[IndexedChunk, ...]) -> None:
        self.chunks = chunks
        self.document_calls: list[str] = []

    async def embed_document(self, text: str) -> tuple[IndexedChunk, ...]:
        self.document_calls.append(text)
        return self.chunks

    async def embed_query(self, text: str) -> tuple[float, ...]:
        raise AssertionError("indexing must not embed queries")


@pytest.mark.asyncio
async def test_index_source_collects_outcome_from_port_chunks() -> None:
    chunk = IndexedChunk(
        chunk_number=0,
        content_sha256="a" * 64,
        text="Заметка про хлеб.",
        embedding=(1.0,) + (0.0,) * 767,
    )
    port = FakeEmbeddingPort((chunk,))
    record_id = uuid4()
    created_at = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    source = IndexingSource(
        record_kind=SearchRecordType.NOTE,
        record_id=record_id,
        text="Заметка про хлеб.",
        content_sha256="b" * 64,
        created_at=created_at,
    )

    outcome = await IndexSource(port).execute(source)

    assert outcome.record_kind is SearchRecordType.NOTE
    assert outcome.record_id == record_id
    assert outcome.chunks == (chunk,)
    assert outcome.content_sha256 == "b" * 64
    assert outcome.created_at == created_at
    assert port.document_calls == ["Заметка про хлеб."]
    assert "хлеб" not in repr(source)
    assert str(record_id) not in repr(source)
    assert "хлеб" not in repr(outcome)
    assert str(record_id) not in repr(outcome)

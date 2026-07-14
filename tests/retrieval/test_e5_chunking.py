import hashlib

import pytest

from second_brain.slices.retrieval.adapters.embedding.e5 import (
    E5EmbeddingModel,
    EmbeddingFailure,
)
from tests.retrieval.embedding_fakes import (
    FakeSentenceTransformersModule,
    install_fake_sentence_transformers,
)

MAX_CHUNK_TOKENS = 448
OVERLAP_TOKENS = 64


def sentences_of_eight_tokens(count: int) -> list[str]:
    return [
        " ".join(f"w{index}x{position}" for position in range(7)) + f" s{index}."
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_short_text_is_a_single_chunk_number_zero_with_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule()
    install_fake_sentence_transformers(monkeypatch, module)

    chunks = await E5EmbeddingModel().embed_document("  Привет,\n\n   мир.  ")

    assert len(chunks) == 1
    assert chunks[0].chunk_number == 0
    assert chunks[0].text == "Привет, мир."
    assert (
        chunks[0].content_sha256 == hashlib.sha256("Привет, мир.".encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_long_text_respects_token_limit_overlap_and_sentence_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule()
    install_fake_sentence_transformers(monkeypatch, module)
    sentences = sentences_of_eight_tokens(120)
    normalized = " ".join(sentences)

    chunks = await E5EmbeddingModel().embed_document(normalized)

    tokenizer = module.created[0].tokenizer
    token_lists = [
        tokenizer.encode(chunk.text, add_special_tokens=False) for chunk in chunks
    ]
    assert len(chunks) > 1
    assert [chunk.chunk_number for chunk in chunks] == list(range(len(chunks)))
    assert all(len(tokens) <= MAX_CHUNK_TOKENS for tokens in token_lists)
    for previous, current in zip(token_lists, token_lists[1:], strict=False):
        assert current[:OVERLAP_TOKENS] == previous[-OVERLAP_TOKENS:]
    assert all(chunk.text.endswith(".") for chunk in chunks)
    reconstructed = list(token_lists[0])
    for tokens in token_lists[1:]:
        reconstructed.extend(tokens[OVERLAP_TOKENS:])
    assert tokenizer.decode(reconstructed) == normalized


@pytest.mark.asyncio
async def test_oversized_sentence_is_hard_split_by_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeSentenceTransformersModule()
    install_fake_sentence_transformers(monkeypatch, module)
    normalized = " ".join(f"u{index}" for index in range(1000))

    chunks = await E5EmbeddingModel().embed_document(normalized)

    tokenizer = module.created[0].tokenizer
    token_lists = [
        tokenizer.encode(chunk.text, add_special_tokens=False) for chunk in chunks
    ]
    assert [chunk.chunk_number for chunk in chunks] == list(range(len(chunks)))
    assert all(len(tokens) <= MAX_CHUNK_TOKENS for tokens in token_lists)
    for previous, current in zip(token_lists, token_lists[1:], strict=False):
        assert current[:OVERLAP_TOKENS] == previous[-OVERLAP_TOKENS:]
    reconstructed = list(token_lists[0])
    for tokens in token_lists[1:]:
        reconstructed.extend(tokens[OVERLAP_TOKENS:])
    assert tokenizer.decode(reconstructed) == normalized


@pytest.mark.asyncio
async def test_same_input_produces_byte_identical_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = " ".join(sentences_of_eight_tokens(120))
    runs: list[tuple[tuple[int, str, str, tuple[float, ...]], ...]] = []
    for _attempt in range(2):
        module = FakeSentenceTransformersModule()
        install_fake_sentence_transformers(monkeypatch, module)
        chunks = await E5EmbeddingModel().embed_document(text)
        runs.append(
            tuple(
                (chunk.chunk_number, chunk.content_sha256, chunk.text, chunk.embedding)
                for chunk in chunks
            )
        )

    assert runs[0] == runs[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   \n\t  "])
async def test_empty_or_whitespace_text_fails_safely_without_loading_model(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    module = FakeSentenceTransformersModule()
    imported = install_fake_sentence_transformers(monkeypatch, module)

    with pytest.raises(EmbeddingFailure) as failure:
        await E5EmbeddingModel().embed_document(text)

    assert failure.value.safe_error_code == "empty_source"
    assert imported == []
    assert module.created == []

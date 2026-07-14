from typing import Protocol

from second_brain.slices.retrieval.domain.entities import IndexedChunk


class EmbeddingModel(Protocol):
    async def embed_document(self, text: str) -> tuple[IndexedChunk, ...]: ...

    async def embed_query(self, text: str) -> tuple[float, ...]: ...

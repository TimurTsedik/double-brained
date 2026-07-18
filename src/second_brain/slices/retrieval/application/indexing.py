from second_brain.slices.retrieval.application.contracts import (
    IndexingOutcome,
    IndexingSource,
)
from second_brain.slices.retrieval.ports.embedding import EmbeddingModel


class IndexSource:
    def __init__(self, embedding_model: EmbeddingModel) -> None:
        self._embedding_model = embedding_model

    async def execute(self, source: IndexingSource) -> IndexingOutcome:
        chunks = await self._embedding_model.embed_document(source.text)
        return IndexingOutcome(
            record_kind=source.record_kind,
            record_id=source.record_id,
            chunks=chunks,
            content_sha256=source.content_sha256,
            created_at=source.created_at,
        )

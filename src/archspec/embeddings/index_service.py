"""按需建立并复用规范片段的本地向量索引。"""

from dataclasses import dataclass

from archspec.documents.repository import DocumentChunkRepository
from archspec.embeddings.provider import EmbeddingProvider
from archspec.embeddings.repository import EmbeddingRepository


@dataclass(frozen=True)
class IndexingSummary:
    indexed_documents: int
    reused_documents: int
    vector_count: int


class EmbeddingIndexService:
    def __init__(
        self,
        chunk_repository: DocumentChunkRepository,
        embedding_repository: EmbeddingRepository,
        provider: EmbeddingProvider,
    ) -> None:
        self.chunk_repository = chunk_repository
        self.embedding_repository = embedding_repository
        self.provider = provider

    def ensure_index(self, document_ids: list[str]) -> IndexingSummary:
        from archspec.standard_governance.compatibility import (
            ContentUseDenied,
            LegacyAdmissionAdapter,
        )

        adapter = LegacyAdmissionAdapter(self.chunk_repository.session)
        indexed = 0
        reused = 0
        vector_count = 0
        for document_id in dict.fromkeys(document_ids):
            try:
                for operation in ("LOCAL_SEARCH", "LOCAL_EXCERPT"):
                    adapter.assert_document_read(document_id, operation)
            except ContentUseDenied:
                # 本地索引也不能绕过明确拒绝；旧索引保留，但不读取、复用或重建。
                continue
            chunks = self.chunk_repository.list_for_document(document_id)
            if not chunks:
                continue
            stored = self.embedding_repository.list_for_document(
                document_id, self.provider.model_name
            )
            current_ids = {chunk.id for chunk in chunks}
            stored_ids = {item.chunk_id for item in stored}
            dimensions_match = all(
                item.dimension == self.provider.dimension for item in stored
            )
            if stored_ids == current_ids and dimensions_match:
                reused += 1
                vector_count += len(stored)
                continue

            vectors = self.provider.embed_documents([chunk.text for chunk in chunks])
            self.embedding_repository.replace_for_document(
                document_id,
                self.provider.model_name,
                [(chunk.id, vector) for chunk, vector in zip(chunks, vectors, strict=True)],
            )
            indexed += 1
            vector_count += len(vectors)
        return IndexingSummary(indexed, reused, vector_count)

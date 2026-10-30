"""使用本地文字向量和余弦相似度搜索规范条文。"""

import math
from dataclasses import dataclass

from archspec.documents.models import DocumentChunkRecord
from archspec.documents.repository import DocumentChunkRepository
from archspec.embeddings.index_service import EmbeddingIndexService
from archspec.embeddings.provider import EmbeddingProvider
from archspec.embeddings.repository import EmbeddingRepository


@dataclass(frozen=True)
class SemanticSearchHit:
    chunk: DocumentChunkRecord
    score: float


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("无法比较维度不同的向量")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


class SemanticSearchService:
    def __init__(
        self,
        chunk_repository: DocumentChunkRepository,
        embedding_repository: EmbeddingRepository,
        index_service: EmbeddingIndexService,
        provider: EmbeddingProvider,
    ) -> None:
        self.chunk_repository = chunk_repository
        self.embedding_repository = embedding_repository
        self.index_service = index_service
        self.provider = provider

    def search_many(
        self,
        document_ids: list[str],
        query: str,
        top_k: int,
    ) -> list[SemanticSearchHit]:
        if top_k < 1 or not query.strip():
            return []
        from archspec.standard_governance.compatibility import (
            ContentUseDenied,
            LegacyAdmissionAdapter,
        )

        adapter = LegacyAdmissionAdapter(self.chunk_repository.session)
        unique_ids = []
        for document_id in dict.fromkeys(document_ids):
            try:
                for operation in ("LOCAL_SEARCH", "LOCAL_EXCERPT"):
                    adapter.assert_document_read(document_id, operation)
            except ContentUseDenied:
                continue
            unique_ids.append(document_id)
        if not unique_ids:
            return []
        self.index_service.ensure_index(unique_ids)
        chunks = {
            chunk.id: chunk
            for document_id in unique_ids
            for chunk in self.chunk_repository.list_for_document(document_id)
        }
        # 空候选无需加载本地模型，更不能无意义地生成查询向量。
        if not chunks:
            return []
        query_vector = self.provider.embed_query(query)
        hits: list[SemanticSearchHit] = []
        for document_id in unique_ids:
            for embedding in self.embedding_repository.list_for_document(
                document_id, self.provider.model_name
            ):
                chunk = chunks.get(embedding.chunk_id)
                if chunk is None:
                    continue
                hits.append(
                    SemanticSearchHit(
                        chunk=chunk,
                        score=round(
                            cosine_similarity(query_vector, embedding.vector), 6
                        ),
                    )
                )
        hits.sort(
            key=lambda item: (
                -item.score,
                item.chunk.document_id,
                item.chunk.chunk_index,
            )
        )
        return hits[:top_k]

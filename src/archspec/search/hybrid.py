"""使用 RRF 合并关键词排名和语义排名。"""

from dataclasses import dataclass
from typing import Protocol

from archspec.documents.models import DocumentChunkRecord
from archspec.search.bm25 import SearchHit
from archspec.search.semantic import SemanticSearchHit


@dataclass(frozen=True)
class HybridSearchHit:
    chunk: DocumentChunkRecord
    score: float
    keyword_rank: int | None
    semantic_rank: int | None


def reciprocal_rank_fusion(
    keyword_hits: list[SearchHit],
    semantic_hits: list[SemanticSearchHit],
    top_k: int,
    rank_constant: int = 60,
) -> list[HybridSearchHit]:
    if top_k < 1:
        return []
    chunks: dict[str, DocumentChunkRecord] = {}
    scores: dict[str, float] = {}
    keyword_ranks: dict[str, int] = {}
    semantic_ranks: dict[str, int] = {}

    for rank, hit in enumerate(keyword_hits, start=1):
        identifier = hit.chunk.id
        chunks[identifier] = hit.chunk
        keyword_ranks[identifier] = rank
        scores[identifier] = scores.get(identifier, 0.0) + 1 / (
            rank_constant + rank
        )
    for rank, hit in enumerate(semantic_hits, start=1):
        identifier = hit.chunk.id
        chunks[identifier] = hit.chunk
        semantic_ranks[identifier] = rank
        scores[identifier] = scores.get(identifier, 0.0) + 1 / (
            rank_constant + rank
        )

    results = [
        HybridSearchHit(
            chunk=chunk,
            score=round(scores[identifier], 8),
            keyword_rank=keyword_ranks.get(identifier),
            semantic_rank=semantic_ranks.get(identifier),
        )
        for identifier, chunk in chunks.items()
    ]
    results.sort(
        key=lambda item: (
            -item.score,
            min(
                rank
                for rank in (item.keyword_rank, item.semantic_rank)
                if rank is not None
            ),
            item.chunk.document_id,
            item.chunk.chunk_index,
        )
    )
    return results[:top_k]


class _KeywordSearch(Protocol):
    def search_many(
        self, document_ids: list[str], query: str, top_k: int
    ) -> list[SearchHit]: ...


class _SemanticSearch(Protocol):
    def search_many(
        self, document_ids: list[str], query: str, top_k: int
    ) -> list[SemanticSearchHit]: ...


class HybridSearchService:
    def __init__(
        self,
        keyword_service: _KeywordSearch,
        semantic_service: _SemanticSearch,
        candidate_k: int = 20,
    ) -> None:
        self.keyword_service = keyword_service
        self.semantic_service = semantic_service
        self.candidate_k = candidate_k

    def search_many(
        self,
        document_ids: list[str],
        query: str,
        top_k: int,
    ) -> list[HybridSearchHit]:
        keyword_hits = self.keyword_service.search_many(
            document_ids, query, self.candidate_k
        )
        semantic_hits = self.semantic_service.search_many(
            document_ids, query, self.candidate_k
        )
        return reciprocal_rank_fusion(keyword_hits, semantic_hits, top_k)

"""关键词检索业务流程：确认文档可用，再对该文档的小卡片排序。"""

from archspec.documents.models import DocumentStatus
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.search.bm25 import BM25Searcher, SearchHit


class SearchService:
    """把文档状态检查、片段读取和 BM25 排序串成一次完整检索。"""

    def __init__(
        self,
        document_repository: DocumentRepository,
        chunk_repository: DocumentChunkRepository,
        searcher: BM25Searcher,
    ) -> None:
        self.document_repository = document_repository
        self.chunk_repository = chunk_repository
        self.searcher = searcher

    def search(
        self,
        document_id: str,
        query: str,
        top_k: int,
    ) -> list[SearchHit] | None:
        """返回相关条文；None 专门表示文档本身不存在。"""
        document = self.document_repository.get(document_id)
        if document is None:
            return None
        if document.status != DocumentStatus.PARSED:
            return []

        self._check_read(document_id)

        chunks = self.chunk_repository.list_for_document(document_id)
        return self.searcher.search(query, chunks, top_k)

    def search_many(
        self,
        document_ids: list[str],
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        """把多份已解析文档的片段放入同一个候选池后统一排序。"""
        chunks = []
        from archspec.standard_governance.compatibility import ContentUseDenied
        for document_id in dict.fromkeys(document_ids):
            document = self.document_repository.get(document_id)
            if document is None or document.status != DocumentStatus.PARSED:
                continue
            try:
                self._check_read(document_id)
            except ContentUseDenied:
                continue
            chunks.extend(self.chunk_repository.list_for_document(document_id))
        return self.searcher.search(query, chunks, top_k)

    def _check_read(self, document_id):
        from archspec.standard_governance.compatibility import LegacyAdmissionAdapter
        adapter = LegacyAdmissionAdapter(self.document_repository.session)
        for operation in ("LOCAL_SEARCH", "LOCAL_EXCERPT"):
            adapter.assert_document_read(document_id, operation)

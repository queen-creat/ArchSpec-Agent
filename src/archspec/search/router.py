"""规范关键词检索 REST API（通过网址调用的接口）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from archspec.database import get_session
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.search.bm25 import BM25Searcher
from archspec.search.schemas import SearchHitResponse, SearchRequest
from archspec.search.service import SearchService
from archspec.search.tokenizer import LexicalTokenizer

router = APIRouter(prefix="/api/search", tags=["search"])


def get_search_service(
    session: Annotated[Session, Depends(get_session)],
) -> SearchService:
    """组装检索需要的数据库读取器、分词器和排序器。"""
    return SearchService(
        DocumentRepository(session),
        DocumentChunkRepository(session),
        BM25Searcher(LexicalTokenizer()),
    )


@router.post("", response_model=list[SearchHitResponse])
def search_document(
    request: SearchRequest,
    service: Annotated[SearchService, Depends(get_search_service)],
) -> list[SearchHitResponse]:
    """只从指定文档中寻找与问题最相关的原文条文。"""
    hits = service.search(request.document_id, request.query, request.top_k)
    if hits is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return [SearchHitResponse.from_hit(hit) for hit in hits]

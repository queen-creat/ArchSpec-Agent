"""检索评测 REST API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from archspec.database import get_session
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.embeddings.dependencies import default_embedding_provider
from archspec.embeddings.index_service import EmbeddingIndexService
from archspec.embeddings.repository import EmbeddingRepository
from archspec.evaluation.models import EvaluationCaseStatus
from archspec.evaluation.repository import EvaluationRepository
from archspec.evaluation.schemas import (
    CreateEvaluationCaseRequest,
    CreateEvaluationRunRequest,
    EvaluationCaseResponse,
    EvaluationRunDetailResponse,
)
from archspec.evaluation.service import (
    CaseNotFound,
    DocumentNotFound,
    EvaluationService,
    EvaluationValidationError,
    NoVerifiedCases,
)
from archspec.search.bm25 import BM25Searcher
from archspec.search.hybrid import HybridSearchService
from archspec.search.semantic import SemanticSearchService
from archspec.search.service import SearchService
from archspec.search.tokenizer import LexicalTokenizer

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])


def get_evaluation_service(
    session: Annotated[Session, Depends(get_session)],
) -> EvaluationService:
    document_repository = DocumentRepository(session)
    chunk_repository = DocumentChunkRepository(session)
    search_service = SearchService(
        document_repository,
        chunk_repository,
        BM25Searcher(LexicalTokenizer()),
    )
    embedding_repository = EmbeddingRepository(session)
    provider = default_embedding_provider
    index_service = EmbeddingIndexService(
        chunk_repository, embedding_repository, provider
    )
    semantic_service = SemanticSearchService(
        chunk_repository,
        embedding_repository,
        index_service,
        provider,
    )
    hybrid_service = HybridSearchService(search_service, semantic_service)
    return EvaluationService(
        document_repository,
        chunk_repository,
        EvaluationRepository(session),
        search_service,
        semantic_service,
        hybrid_service,
    )


@router.post(
    "/cases",
    response_model=EvaluationCaseResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_case(
    request: CreateEvaluationCaseRequest,
    service: Annotated[EvaluationService, Depends(get_evaluation_service)],
) -> EvaluationCaseResponse:
    try:
        case = service.create_case(
            request.document_id,
            request.query,
            request.expected_clause_numbers,
            request.source_note,
            request.status,
        )
    except DocumentNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except EvaluationValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return EvaluationCaseResponse.from_record(case)


@router.get("/cases", response_model=list[EvaluationCaseResponse])
def list_cases(
    service: Annotated[EvaluationService, Depends(get_evaluation_service)],
    document_id: Annotated[str | None, Query()] = None,
    case_status: Annotated[
        EvaluationCaseStatus | None, Query(alias="status")
    ] = None,
) -> list[EvaluationCaseResponse]:
    return [
        EvaluationCaseResponse.from_record(case)
        for case in service.list_cases(document_id, case_status)
    ]


@router.get("/cases/{case_id}", response_model=EvaluationCaseResponse)
def get_case(
    case_id: str,
    service: Annotated[EvaluationService, Depends(get_evaluation_service)],
) -> EvaluationCaseResponse:
    try:
        return EvaluationCaseResponse.from_record(service.get_case(case_id))
    except CaseNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/runs",
    response_model=EvaluationRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_run(
    request: CreateEvaluationRunRequest,
    service: Annotated[EvaluationService, Depends(get_evaluation_service)],
) -> EvaluationRunDetailResponse:
    try:
        run = service.run(request.document_id, request.algorithm)
    except DocumentNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except NoVerifiedCases as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except EvaluationValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    detail = service.get_run(run.id)
    if detail is None:
        raise HTTPException(status_code=500, detail="评测结果保存失败")
    return EvaluationRunDetailResponse.from_records(*detail)


@router.get("/runs/{run_id}", response_model=EvaluationRunDetailResponse)
def get_run(
    run_id: str,
    service: Annotated[EvaluationService, Depends(get_evaluation_service)],
) -> EvaluationRunDetailResponse:
    detail = service.get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    return EvaluationRunDetailResponse.from_records(*detail)
